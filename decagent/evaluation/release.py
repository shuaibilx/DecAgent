"""Release evidence integrity; a checked receipt remains a claim made by its producer."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from decagent.policy.redaction import Redactor

REQUIRED_CHECKS = (
    "static",
    "offline",
    "database",
    "container",
    "compose",
    "live_main",
    "live_jev",
    "repository",
    "recovery",
    "backup_rollback",
    "windows",
    "native_linux",
    "comparison",
)
SHA256 = re.compile(r"[a-f0-9]{64}")


def source_digest(root):
    """Bind evidence to implementation/configuration; documentation does not change execution."""
    root = Path(root).resolve()
    files = []
    for directory in ("decagent", "ops"):
        files.extend(
            p
            for p in (root / directory).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
        )
    files.extend(
        root / name
        for name in (
            "pyproject.toml",
            "requirements.txt",
            "requirements.lock",
            "compose.yaml",
            ".dockerignore",
        )
    )
    value = hashlib.sha256()
    for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Implementation files must stay within the checkout")
        value.update(path.relative_to(root).as_posix().encode() + b"\0")
        value.update(hashlib.sha256(path.read_bytes()).digest())
    return value.hexdigest()


def assess_release(manifest, evidence_root, current_code_digest):
    if (
        type(manifest) is not dict
        or manifest.get("format_version") != 1
        or type(manifest.get("checks")) is not dict
        or not SHA256.fullmatch(current_code_digest)
    ):
        raise ValueError("Invalid release manifest")
    root = Path(evidence_root).resolve()
    results = {}
    for name in REQUIRED_CHECKS:
        receipt = manifest["checks"].get(name)
        reasons = []
        if type(receipt) is not dict:
            results[name] = {"status": "missing", "reasons": ["No evidence receipt"]}
            continue
        if receipt.get("status") != "passed":
            reasons.append("Check did not pass")
        if (
            manifest.get("code_digest") != current_code_digest
            or receipt.get("code_digest") != current_code_digest
        ):
            reasons.append("Evidence belongs to a different implementation")
        if type(receipt.get("passed")) is not int or receipt["passed"] <= 0:
            reasons.append("No successful assertions recorded")
        if type(receipt.get("skipped")) is not int or receipt["skipped"] != 0:
            reasons.append("Skipped or unknown assertions remain")
        try:
            relative = Path(receipt["evidence"])
            path = root / relative
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not path.resolve().is_relative_to(root)
                or any(p.is_symlink() for p in (path, *path.parents) if p != root.parent)
                or path.stat().st_size > 32 * 1024 * 1024
            ):
                raise ValueError("Unsafe evidence path")
            expected = receipt["sha256"]
            if not isinstance(expected, str) or not SHA256.fullmatch(expected):
                raise ValueError("Invalid evidence hash")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("Evidence changed")
        except (KeyError, TypeError, OSError, ValueError):
            reasons.append("Evidence is unavailable, unsafe or modified")
        results[name] = {"status": "blocked" if reasons else "passed", "reasons": reasons}
    blocked = [key for key, value in results.items() if value["status"] != "passed"]
    return {
        "format_version": 1,
        "status": "incomplete" if blocked else "passed",
        "code_digest": current_code_digest,
        "checks": results,
        "blocked": blocked,
        "scope": "Full first-release verification, including pending native Linux and comparison",
    }


def load_and_assess(path, root):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    return assess_release(document, root, source_digest(root))


def export_release_report(root, report, *, now=None):
    from datetime import UTC, datetime, timedelta, timezone

    local = (now or datetime.now(UTC)).astimezone(timezone(timedelta(hours=8)))
    folder = Path(root) / "generated" / local.strftime("%y%m%d")
    folder.mkdir(parents=True, exist_ok=True)
    stem = local.strftime("%H%M") + "_发布验收报告"
    content = Redactor().redact_text(json.dumps(report, ensure_ascii=False, indent=2))
    for number in range(1, 10000):
        name = stem + (f"_{number:02d}" if number > 1 else "") + ".md"
        try:
            with (folder / name).open("x", encoding="utf-8") as stream:
                stream.write(
                    "# 发布验收报告\n\n任何缺失、跳过、过期或篡改证据都会阻止完整通过。"
                    "证据完整性校验不替代人工核验其真实性。\n\n```json\n" + content + "\n```\n"
                )
            return folder / name
        except FileExistsError:
            continue
    raise FileExistsError("No release report filename is available")
