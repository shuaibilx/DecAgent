"""Content addressed evidence, sanitized before persistence and verified on every read."""

import hashlib
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from decagent.policy import Redactor


@dataclass(frozen=True)
class EvidenceRef:
    sha256: str
    size: int
    source: str


class EvidenceStore:
    def __init__(self, root: Path, redact: Callable[[str], str] | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.redact = redact or self._default_redact

    @staticmethod
    def _default_redact(text):
        return Redactor().redact_text(text)

    def _path(self, digest):
        if not re.fullmatch("[a-f0-9]{64}", digest):
            raise ValueError("Invalid evidence digest")
        path = self.root / digest
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ValueError("Evidence path escapes store")
        return path

    def write(self, text: str, source: str) -> EvidenceRef:
        data = self.redact(text).encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        target = self._path(digest)
        if target.exists():
            self.read(EvidenceRef(digest, len(data), source))
        else:
            descriptor, temp_path = tempfile.mkstemp(dir=self.root, prefix=".pending-")
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, target)
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
        return EvidenceRef(digest, len(data), source)

    def read(self, reference: EvidenceRef) -> str:
        data = self._path(reference.sha256).read_bytes()
        if len(data) != reference.size or hashlib.sha256(data).hexdigest() != reference.sha256:
            raise ValueError("Evidence integrity verification failed")
        return data.decode("utf-8")
