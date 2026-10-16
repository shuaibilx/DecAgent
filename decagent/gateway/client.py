"""CLI owner client. Credential content is loaded from a local file, never CLI arguments."""

from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from decagent.policy import Redactor


class OwnerClient:
    def __init__(self, base_url, credential_path, *, transport=None, timeout=30):
        parts = urlsplit(base_url)
        if parts.scheme != "https" and not (
            parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError("Remote owner client requires HTTPS")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Owner URL must not contain credentials or query parameters")
        token = Path(credential_path).expanduser().read_text(encoding="utf-8").strip()
        if not token.startswith("dca_") or not 43 <= len(token) <= 512:
            raise ValueError("Invalid local owner credential file")
        self.redactor = Redactor(known_secrets=(token,))
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": "Bearer " + token},
            transport=transport,
            follow_redirects=False,
        )

    def request(self, method, path, *, body=None, idempotency_key=None):
        headers = {}
        if method not in {"GET", "HEAD"}:
            headers["Idempotency-Key"] = idempotency_key or str(uuid4())
        try:
            response = self.client.request(method, "/api/v1" + path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise RuntimeError("Owner API is unreachable; run decagent doctor") from exc
        if response.status_code >= 400:
            try:
                failure = response.json()
            except ValueError:
                failure = {}
            code = self.redactor.redact_text(str(failure.get("code", "request_failed")))
            message = self.redactor.redact_text(
                str(failure.get("message", "Owner API request failed"))
            )
            raise RuntimeError(f"{response.status_code} {code}: {message}")
        if response.status_code >= 300:
            raise RuntimeError("Owner API redirect refused; verify configured origin")
        return response.json()

    def task(self, task_id):
        return self.request("GET", f"/tasks/{task_id}")

    def tasks(self):
        return self.request("GET", "/tasks")

    def cancel(self, task_id, *, idempotency_key=None):
        return self.request(
            "POST", f"/tasks/{task_id}/cancel", body={}, idempotency_key=idempotency_key
        )

    def resume(self, task_id, *, budget=None, idempotency_key=None):
        return self.request(
            "POST",
            f"/tasks/{task_id}/resume",
            body={"budget": budget},
            idempotency_key=idempotency_key,
        )

    def diagnostics(self, *, mode="local", target=None, confirm_external_request=False):
        return self.request(
            "POST",
            "/diagnostics",
            body={
                "mode": mode,
                "target": target,
                "confirm_external_request": confirm_external_request,
            },
        )

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
