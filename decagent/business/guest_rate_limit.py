"""Persistent anonymous guest admission limits and trusted proxy resolution."""

import hashlib
import hmac
import ipaddress
import math
from dataclasses import dataclass
from datetime import timedelta

SESSION_CREATE_LIMIT = 5
SESSION_CREATE_WINDOW_SECONDS = 60 * 60
TASK_SUBMIT_LIMIT = 10
TASK_SUBMIT_WINDOW_SECONDS = 60
MANUAL_REPLY_LIMIT = 30
MANUAL_REPLY_WINDOW_SECONDS = 60
RATE_LIMIT_SCOPES = frozenset({"guest_create", "guest_task_submit", "guest_manual_reply"})
_UNKNOWN_CLIENT = "unknown"


def _networks(values):
    return tuple(ipaddress.ip_network(value, strict=False) for value in values)


def _parse_ip(value):
    try:
        return ipaddress.ip_address(value)
    except (ValueError, TypeError):
        return None


def resolve_client_ip(peer_address, forwarded_for, trusted_proxies=()):
    """Resolve X-Forwarded-For from the right through only explicitly trusted peers."""
    peer = _parse_ip(peer_address)
    if peer is None:
        return _UNKNOWN_CLIENT
    networks = _networks(trusted_proxies)

    def trusted(address):
        return any(
            address.version == network.version and address in network for network in networks
        )

    peer_text = peer.compressed
    if not trusted(peer) or not isinstance(forwarded_for, str) or not forwarded_for:
        return peer_text
    if len(forwarded_for) > 2048:
        return peer_text
    parts = [part.strip() for part in forwarded_for.split(",")]
    if not parts or len(parts) > 16:
        return peer_text
    chain = [_parse_ip(part) for part in parts]
    if any(address is None for address in chain):
        return peer_text

    current = peer
    for address in reversed(chain):
        if not trusted(current):
            break
        current = address
    if trusted(current):
        # An all-trusted chain has no client boundary to anchor on. Do not
        # trust the leftmost forwarded value; retain the authenticated peer.
        return peer_text
    return current.compressed


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class GuestRateLimitKeyRotationPending(RuntimeError):
    def __init__(self, retry_after_seconds):
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        super().__init__("Guest rate-limit key rotation must wait until prior windows expire")


class GuestAdmissionRateLimiter:
    """Database-backed sliding windows shared by API processes and restarts."""

    def __init__(self, database, *, hmac_key=None):
        if hmac_key is not None and (not isinstance(hmac_key, bytes) or len(hmac_key) != 32):
            raise ValueError("Guest rate-limit HMAC secret must be exactly 32 bytes")
        self.database = database
        self.hmac_key = hmac_key

    def check(self, *, scope, client_ip, limit, window_seconds):
        if scope not in RATE_LIMIT_SCOPES:
            raise ValueError("Unsupported guest rate-limit scope")
        if type(limit) is not int or limit <= 0:
            raise ValueError("Rate limit must be positive")
        if type(window_seconds) is not int or window_seconds <= 0:
            raise ValueError("Rate-limit window must be positive")
        client = _parse_ip(client_ip)
        normalized_client = client.compressed if client else _UNKNOWN_CLIENT
        if self.hmac_key is None:
            raise RuntimeError("Guest rate-limit HMAC secret is not configured")

        with self.database.connect() as conn:
            self._ensure_key_fingerprint(conn)
            subject_hash = hmac.new(
                self.hmac_key, normalized_client.encode("ascii"), hashlib.sha256
            ).digest()
            lock_key = hmac.new(
                self.hmac_key, f"{scope}:".encode("ascii") + subject_hash, hashlib.sha256
            ).hexdigest()
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,7731405))", (lock_key,))
            conn.execute(
                "DELETE FROM business.guest_rate_limit_events "
                "WHERE occurred_at < clock_timestamp()-interval '2 hours'"
            )
            row = conn.execute(
                "SELECT count(*) AS hits,min(occurred_at) AS oldest,"
                "clock_timestamp() AS checked_at FROM business.guest_rate_limit_events "
                "WHERE scope=%s AND subject_hash=%s AND occurred_at>"
                "clock_timestamp()-(%s*interval '1 second')",
                (scope, subject_hash, window_seconds),
            ).fetchone()
            hits = int(row["hits"])
            if hits >= limit:
                expires_at = row["oldest"] + timedelta(seconds=window_seconds)
                retry_after = max(1, math.ceil((expires_at - row["checked_at"]).total_seconds()))
                return RateLimitDecision(False, 0, retry_after)

            conn.execute(
                "INSERT INTO business.guest_rate_limit_events(scope,subject_hash) VALUES(%s,%s)",
                (scope, subject_hash),
            )
            return RateLimitDecision(True, max(0, limit - hits - 1), 0)

    def _ensure_key_fingerprint(self, conn):
        fingerprint = hmac.new(
            self.hmac_key,
            b"decagent:guest-rate-limit-key-fingerprint:v1",
            hashlib.sha256,
        ).digest()
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,7731406))",
            ("guest_rate_limit_key_state",),
        )
        row = conn.execute(
            "SELECT key_fingerprint FROM business.guest_rate_limit_key_state "
            "WHERE singleton=true FOR UPDATE"
        ).fetchone()
        if row and hmac.compare_digest(row["key_fingerprint"], fingerprint):
            return

        recent = conn.execute(
            "SELECT max(occurred_at) AS latest,clock_timestamp() AS checked_at "
            "FROM business.guest_rate_limit_events "
            "WHERE occurred_at>clock_timestamp()-interval '2 hours'"
        ).fetchone()
        if recent["latest"] is not None:
            retry_after = math.ceil(
                (recent["latest"] + timedelta(hours=2) - recent["checked_at"]).total_seconds()
            )
            raise GuestRateLimitKeyRotationPending(retry_after)

        if row:
            conn.execute(
                "UPDATE business.guest_rate_limit_key_state "
                "SET key_fingerprint=%s,activated_at=clock_timestamp() WHERE singleton=true",
                (fingerprint,),
            )
        else:
            conn.execute(
                "INSERT INTO business.guest_rate_limit_key_state(singleton,key_fingerprint) "
                "VALUES(true,%s)",
                (fingerprint,),
            )


class TrustedProxyHeadersMiddleware:
    """Apply forwarded client/scheme headers only when the TCP peer is trusted."""

    def __init__(self, app, *, trusted_proxies=()):
        self.app = app
        self.trusted_proxies = tuple(trusted_proxies)
        self.networks = _networks(self.trusted_proxies)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", ())
        forwarded_for = ",".join(
            value.decode("latin-1") for name, value in headers if name.lower() == b"x-forwarded-for"
        )
        peer = (scope.get("client") or (None, None))[0]
        client_ip = resolve_client_ip(peer, forwarded_for, self.trusted_proxies)
        state = dict(scope.get("state") or {})
        state["guest_client_ip"] = client_ip

        parsed_peer = _parse_ip(peer)
        peer_trusted = bool(
            parsed_peer
            and any(
                parsed_peer.version == network.version and parsed_peer in network
                for network in self.networks
            )
        )
        forwarded_proto = [
            value.decode("latin-1").strip().lower()
            for name, value in headers
            if name.lower() == b"x-forwarded-proto"
        ]
        rewritten_scope = dict(scope)
        rewritten_scope["state"] = state
        if peer_trusted and len(forwarded_proto) == 1 and forwarded_proto[0] in {"http", "https"}:
            rewritten_scope["scheme"] = forwarded_proto[0]
        await self.app(rewritten_scope, receive, send)
