"""HTTP/CONNECT proxy with fresh DNS validation and connections pinned to literal IPs.

Each HTTP connection forwards one request then closes, so follow-up requests and
redirects must be independently validated. CONNECT supports TLS to port 443 only.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import socket
from urllib.parse import urlsplit

BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "100.64.0.0/10",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "2002::/16",
        "2001::/32",
    )
)
BLOCKED_NAMES = frozenset(
    {
        "localhost",
        "host.docker.internal",
        "gateway.docker.internal",
        "metadata",
        "metadata.google.internal",
        "postgres",
        "executor-broker",
        "egress-proxy",
        "api",
        "worker",
    }
)
HEADER_LIMIT = 32768


def validate_address(value: str) -> str:
    address = ipaddress.ip_address(value)
    if (
        not address.is_global
        or address.is_multicast
        or address.is_unspecified
        or (isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None)
        or any(address.version == net.version and address in net for net in BLOCKED_NETWORKS)
    ):
        raise ValueError("Target address is forbidden")
    return str(address)


def validate_host(host: str) -> str:
    if not host or "%" in host or any(ord(char) < 33 for char in host):
        raise ValueError("Invalid host")
    host = host.rstrip(".").lower()
    if host in BLOCKED_NAMES or host.endswith((".internal", ".local", ".localhost")):
        raise ValueError("Control or local hostname forbidden")
    try:
        return validate_address(host)
    except ValueError:
        if ":" in host or re.fullmatch(r"[0-9.]+", host):
            raise ValueError("Forbidden address") from None
    if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        raise ValueError("Public DNS hostname required")
    return host


def parse_target(url: str):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ValueError("Only credential-free HTTP/HTTPS targets are supported")
    if parsed.fragment or any(char in url for char in "\r\n\0"):
        raise ValueError("Invalid target")
    host = validate_host(parsed.hostname or "")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in {80, 443} or (parsed.scheme == "https" and port != 443):
        raise ValueError("Only public HTTP/HTTPS ports are supported")
    return host, port, (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")


async def dns_resolve(host, port):
    answers = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({answer[4][0] for answer in answers})


class TargetPolicy:
    def __init__(self, resolver=dns_resolve):
        self.resolver = resolver

    async def resolve(self, host, port):
        host = validate_host(host)
        if port not in {80, 443}:
            raise ValueError("Unsupported target port")
        addresses = await asyncio.wait_for(self.resolver(host, port), timeout=10)
        if not addresses:
            raise ValueError("Target has no addresses")
        # Reject the entire answer set, rather than choosing a convenient public answer.
        return [validate_address(address) for address in addresses]

    async def connect(self, host, port):
        addresses = await self.resolve(host, port)
        for address in addresses:
            try:
                # Literal IP prevents the connection layer from resolving the name again.
                return await asyncio.wait_for(asyncio.open_connection(address, port), timeout=10)
            except (OSError, TimeoutError):
                continue
        raise OSError("Public target is unavailable")


async def relay(reader, writer):
    while data := await asyncio.wait_for(reader.read(65536), timeout=30):
        writer.write(data)
        await writer.drain()
    with contextlib.suppress(OSError, RuntimeError):
        writer.write_eof()


class ForwardProxy:
    def __init__(self, policy=None, max_connections=128):
        self.policy = policy or TargetPolicy()
        self.capacity = asyncio.Semaphore(max_connections)

    async def handle(self, reader, writer):
        upstream = None
        accepted = False
        try:
            async with self.capacity:
                header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
                if len(header) > HEADER_LIMIT:
                    raise ValueError("Headers exceed limit")
                lines = header.decode("latin1").split("\r\n")
                method, target, version = lines[0].split(" ")
                if version not in {"HTTP/1.0", "HTTP/1.1"}:
                    raise ValueError("Invalid HTTP version")
                headers = []
                names = set()
                for line in lines[1:-2]:
                    name, value = line.split(":", 1)
                    lower = name.lower()
                    if not re.fullmatch(r"[A-Za-z0-9-]+", name) or lower in names:
                        raise ValueError("Malformed or duplicate header")
                    names.add(lower)
                    headers.append((lower, value.strip()))
                if method == "CONNECT":
                    host, port, _ = parse_target("https://" + target)
                    if port != 443 or any(
                        name in {"content-length", "transfer-encoding"} for name, _ in headers
                    ):
                        raise ValueError("Invalid CONNECT")
                    remote, upstream = await self.policy.connect(host, port)
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                else:
                    if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
                        raise ValueError("Unsupported HTTP method")
                    if not target.startswith("http://"):
                        raise ValueError("HTTPS must use CONNECT")
                    host, port, path = parse_target(target)
                    if "transfer-encoding" in names or "upgrade" in names:
                        raise ValueError("Chunked uploads and upgrades are unsupported")
                    length = next(
                        (int(value) for name, value in headers if name == "content-length"), 0
                    )
                    if not 0 <= length <= 50 * 1024 * 1024:
                        raise ValueError("Request body exceeds limit")
                    remote, upstream = await self.policy.connect(host, port)
                    authority = f"[{host}]" if ":" in host else host
                    if port != 80:
                        authority += f":{port}"
                    forwarded = [f"{method} {path} HTTP/1.1", f"Host: {authority}"]
                    excluded = {
                        "host",
                        "connection",
                        "proxy-connection",
                        "proxy-authorization",
                        "keep-alive",
                        "te",
                        "trailer",
                        "expect",
                    }
                    forwarded += [
                        f"{name}: {value}" for name, value in headers if name not in excluded
                    ]
                    forwarded.append("Connection: close")
                    upstream.write(("\r\n".join(forwarded) + "\r\n\r\n").encode("latin1"))
                    remaining = length
                    while remaining:
                        data = await asyncio.wait_for(
                            reader.read(min(remaining, 65536)), timeout=30
                        )
                        if not data:
                            raise ValueError("Incomplete request body")
                        upstream.write(data)
                        await upstream.drain()
                        remaining -= len(data)
                await writer.drain()
                accepted = True
                if method == "CONNECT":
                    tasks = [
                        asyncio.create_task(relay(reader, upstream)),
                        asyncio.create_task(relay(remote, writer)),
                    ]
                    try:
                        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED, timeout=600)
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                else:
                    await asyncio.wait_for(relay(remote, writer), timeout=600)
        except (
            ValueError,
            OSError,
            TimeoutError,
            UnicodeError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            if not accepted:
                writer.write(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                with contextlib.suppress(OSError):
                    await writer.drain()
        finally:
            if upstream is not None:
                upstream.close()
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


async def serve(host="0.0.0.0", port=3128):
    proxy = ForwardProxy()
    server = await asyncio.start_server(proxy.handle, host, port, limit=HEADER_LIMIT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(serve())
