"""Repository-bound GitHub REST access. Tokens never enter a task executor."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from urllib.parse import quote, urlparse
from uuid import UUID

import httpx

from decagent.policy import Redactor
from decagent.policy.paths import normalize_relative_path

API_VERSION = "2026-03-10"
SHA = re.compile(r"[a-f0-9]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class GitHubError(RuntimeError):
    def __init__(self, message, *, status=None, unknown=False):
        super().__init__(message)
        self.status = status
        self.unknown = unknown


class PublicationBlocked(ValueError):
    pass


def require_sha(value):
    if type(value) is not str or not SHA.fullmatch(value):
        raise ValueError("A fixed lowercase commit SHA is required")
    return value


def task_branch(task_id):
    if type(task_id) is not str or str(UUID(task_id)) != task_id:
        raise ValueError("Canonical task UUID required")
    return "decagent/" + task_id


def branch_name(value):
    if (
        type(value) is not str
        or not value
        or len(value) > 200
        or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._/"
            for c in value
        )
        or ".." in value
        or value.startswith(("/", "."))
        or value.endswith(("/", ".", ".lock"))
        or "//" in value
    ):
        raise ValueError("Invalid branch name")
    return value


@dataclass(frozen=True)
class RepositoryDiagnostic:
    repository_id: int
    repository: str
    default_branch: str
    contents_readable: bool
    pull_requests_readable: bool
    # A read-only diagnostic cannot prove fine-grained write permissions.
    write_permissions_verified: bool = False


@dataclass(frozen=True)
class FileChange:
    path: str
    content: bytes | None
    executable: bool = False

    def __post_init__(self):
        object.__setattr__(self, "path", normalize_relative_path(self.path))
        if self.path == "." or type(self.executable) is not bool:
            raise ValueError("A regular file path and boolean mode are required")
        if self.content is not None:
            if type(self.content) is not bytes or len(self.content) > 1_048_576:
                raise ValueError("File content must be bounded bytes")
            text = self.content.decode("utf-8", errors="replace")
            if Redactor(max_text_chars=1_048_576).redact_text(text) != text:
                raise ValueError("Suspected secret in publication content")


@dataclass(frozen=True)
class VerificationGate:
    records: tuple

    @classmethod
    def check(cls, rows, base_sha, tree_hash):
        """Rows must come from trusted verification storage, never model output."""
        require_sha(base_sha)
        if not re.fullmatch(r"[a-f0-9]{64}", tree_hash):
            raise PublicationBlocked("Invalid verified workspace digest")
        baseline = next(
            (r for r in reversed(rows) if r["kind"] == "baseline" and r["base_sha"] == base_sha),
            None,
        )
        if (
            not baseline
            or baseline["status"] != "failed"
            or baseline.get("environment", {}).get("related_failure") is not True
        ):
            raise PublicationBlocked("A relevant baseline failure is required")
        independent = next(
            (
                r
                for r in reversed(rows)
                if r["kind"] == "independent"
                and r["base_sha"] == base_sha
                and r["tree_hash"] == tree_hash
                and r["checker_hash"] == baseline["checker_hash"]
            ),
            None,
        )
        regression = next(
            (
                r
                for r in reversed(rows)
                if r["kind"] == "regression"
                and r["base_sha"] == base_sha
                and r["tree_hash"] == tree_hash
            ),
            None,
        )
        selected = (baseline, independent, regression)
        if any(
            not row
            or not row.get("evidence_hash")
            or type(row["test_count"]) is not int
            or row["test_count"] <= 0
            for row in selected
        ):
            raise PublicationBlocked("Complete nonempty verification evidence is required")
        if independent["status"] != "passed" or regression["status"] != "passed":
            raise PublicationBlocked("Independent and regression verification must pass")
        return cls(selected)


class GitHub:
    """The constructor fixes scope; methods accept only repository-relative endpoints."""

    def __init__(self, repository, token, *, repository_id=None, client=None):
        if type(repository) is not str or not REPOSITORY.fullmatch(repository):
            raise ValueError("Repository must be owner/name")
        if not token or type(token) is not str:
            raise ValueError("GitHub PAT is not configured")
        self.repository = repository
        self.repository_id = repository_id
        self._token = token
        self.client = client or httpx.Client(timeout=15, follow_redirects=False)
        self.redactor = Redactor(known_secrets=(token,))

    def _request(self, method, endpoint, *, payload=None, params=None):
        if (
            type(endpoint) is not str
            or endpoint.startswith("/")
            or ":" in endpoint
            or "?" in endpoint
            or "#" in endpoint
            or "\\" in endpoint
            or any(p in {".", ".."} for p in endpoint.split("/"))
        ):
            raise ValueError("Only repository-relative endpoints are accepted")
        url = "https://api.github.com/repos/" + self.repository
        if endpoint:
            url += "/" + endpoint
        try:
            response = self.client.request(
                method,
                url,
                json=payload,
                params=params,
                headers={
                    "Authorization": "Bearer " + self._token,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                },
                timeout=15,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise GitHubError(
                "GitHub request failed; reconcile before retrying mutations",
                unknown=method != "GET",
            ) from exc
        if response.is_error or response.is_redirect:
            status = response.status_code
            message = (
                "GitHub permissions or repository access are insufficient"
                if status in {401, 403, 404}
                else "GitHub request rejected"
            )
            raise GitHubError(message, status=status, unknown=method != "GET" and status >= 500)
        return response

    def get(self, endpoint, *, params=None):
        return self._json(self._request("GET", endpoint, params=params))

    def post(self, endpoint, payload):
        return self._json(self._request("POST", endpoint, payload=payload))

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubError("Malformed GitHub response", unknown=True) from exc

    def diagnose(self):
        data = self.get("")
        if (
            type(data.get("id")) is not int
            or data.get("full_name", "").casefold() != self.repository.casefold()
        ):
            raise GitHubError("Repository identity differs from registered scope")
        if self.repository_id is not None and data["id"] != self.repository_id:
            raise GitHubError("Repository identity changed; registration must be reviewed")
        self.repository_id = data["id"]
        self.get("contents")
        self.get("pulls", params={"state": "all", "per_page": 1})
        return RepositoryDiagnostic(
            data["id"], self.repository, branch_name(data["default_branch"]), True, True
        )

    def archive(self, sha, *, max_bytes=52_428_800):
        require_sha(sha)
        # GitHub's archive endpoint redirects. Never forward a PAT to the next host.
        url = f"https://api.github.com/repos/{self.repository}/tarball/{sha}"
        headers = {"Authorization": "Bearer " + self._token, "X-GitHub-Api-Version": API_VERSION}
        for _attempt in range(3):
            try:
                with self.client.stream(
                    "GET", url, headers=headers, timeout=30, follow_redirects=False
                ) as response:
                    if response.is_redirect:
                        target = response.headers.get("location", "")
                        parsed = urlparse(target)
                        prefix = "/" + self.repository + "/"
                        if (
                            parsed.scheme != "https"
                            or parsed.hostname != "codeload.github.com"
                            or parsed.port not in (None, 443)
                            or parsed.username
                            or parsed.password
                            or not parsed.path.startswith(prefix)
                            or sha not in parsed.path.split("/")
                        ):
                            raise GitHubError("Unsafe GitHub archive redirect")
                        url, headers = target, {}
                        continue
                    if response.status_code != 200:
                        raise GitHubError(
                            "Cannot download fixed repository archive", status=response.status_code
                        )
                    output = bytearray()
                    for chunk in response.iter_bytes():
                        output.extend(chunk)
                        if len(output) > max_bytes:
                            raise GitHubError("Repository archive exceeds configured limit")
                    return bytes(output)
            except httpx.HTTPError as exc:
                raise GitHubError("Repository archive download failed") from exc
        raise GitHubError("Too many GitHub archive redirects")

    def ref(self, branch):
        branch_name(branch)
        try:
            result = self.get("git/ref/heads/" + quote(branch, safe="/"))
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        return require_sha(result["object"]["sha"])

    def commit(self, sha):
        return self.get("git/commits/" + require_sha(sha))

    def blob(self, content):
        result = self.post(
            "git/blobs", {"content": base64.b64encode(content).decode(), "encoding": "base64"}
        )
        return require_sha(result["sha"])

    def tree(self, base_tree, entries):
        result = self.post("git/trees", {"base_tree": require_sha(base_tree), "tree": entries})
        return require_sha(result["sha"])

    def create_commit(self, message, tree, base, author):
        result = self.post(
            "git/commits",
            {
                "message": message,
                "tree": require_sha(tree),
                "parents": [require_sha(base)],
                "author": author,
                "committer": author,
            },
        )
        return require_sha(result["sha"])

    def create_ref(self, task_id, sha, *, default_branch):
        branch = task_branch(task_id)
        if branch == branch_name(default_branch):
            raise PublicationBlocked("Default branch publication is forbidden")
        self.post("git/refs", {"ref": "refs/heads/" + branch, "sha": require_sha(sha)})
        # There is intentionally no update-ref or merge operation.

    def find_pull(self, task_id, base):
        branch = task_branch(task_id)
        branch_name(base)
        matches = []
        for page in range(1, 11):
            rows = self.get(
                "pulls",
                params={
                    "head": self.repository.split("/")[0] + ":" + branch,
                    "base": base,
                    "state": "all",
                    "per_page": 100,
                    "page": page,
                },
            )
            if type(rows) is not list:
                raise GitHubError("Malformed pull request list")
            for row in rows:
                if (
                    f"<!-- decagent-task:{task_id} -->" in (row.get("body") or "")
                    and row.get("head", {}).get("ref") == branch
                    and row.get("head", {}).get("repo", {}).get("full_name", "").casefold()
                    == self.repository.casefold()
                    and row.get("base", {}).get("ref") == base
                    and row.get("base", {}).get("repo", {}).get("full_name", "").casefold()
                    == self.repository.casefold()
                ):
                    matches.append(row)
            if len(rows) < 100:
                break
        else:
            raise GitHubError("Pull request reconciliation exceeded page limit")
        if len(matches) > 1:
            raise GitHubError("Multiple matching pull requests; owner reconciliation required")
        return matches[0] if matches else None

    def create_pull(self, task_id, base, title, body):
        return self.post(
            "pulls",
            {
                "head": task_branch(task_id),
                "base": branch_name(base),
                "title": self.redactor.redact_text(title)[:240],
                "body": self.redactor.redact_text(body),
                "draft": True,
            },
        )

    def pull(self, number):
        if type(number) is not int or number <= 0:
            raise ValueError("Invalid pull request number")
        return self.get(f"pulls/{number}")

    def source_blobs(self, sha, paths):
        # Files are checked individually at the actual merge SHA, supporting squash/rebase.
        output = {}
        for path in paths:
            normalized = normalize_relative_path(path)
            try:
                data = self.get(
                    "contents/" + quote(normalized, safe="/"), params={"ref": require_sha(sha)}
                )
            except GitHubError as exc:
                if exc.status == 404:
                    output[normalized] = None
                    continue
                raise
            if data.get("type") != "file" or data.get("path") != normalized:
                raise GitHubError("Merge source is not a regular registered file")
            output[normalized] = require_sha(data["sha"])
        return output
