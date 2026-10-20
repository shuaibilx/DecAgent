"""Stable hashes for frozen JSON inputs across Git line-ending policies."""

from hashlib import sha256


def _sha256_with_canonical_newlines(contents: bytes, *, newline: bytes) -> str:
    if type(contents) is not bytes:
        raise TypeError("Frozen manifest content must be bytes")
    if newline not in {b"\n", b"\r\n"}:
        raise ValueError("Canonical newline must be LF or CRLF")
    normalized = contents.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if newline == b"\r\n":
        normalized = normalized.replace(b"\n", b"\r\n")
    return sha256(normalized).hexdigest()


def source_manifest_sha256(contents: bytes) -> str:
    """Hash source manifests as UTF-8 text with canonical LF line endings."""
    return _sha256_with_canonical_newlines(contents, newline=b"\n")


def task_inventory_sha256(contents: bytes) -> str:
    """Hash task inventory as UTF-8 text with canonical CRLF line endings.

    Existing frozen cohort digests were recorded from a Windows CRLF checkout.
    Canonicalizing to CRLF preserves those pinned digests on Linux checkouts too.
    """
    return _sha256_with_canonical_newlines(contents, newline=b"\r\n")
