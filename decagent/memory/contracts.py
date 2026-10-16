"""Strict memory contracts loaded by trusted services, never model authority flags."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from decagent.policy.canonical import digest
from decagent.policy.models import Provenance
from decagent.policy.paths import normalize_relative_path
from decagent.storage.evidence import EvidenceRef


def _sha(value, lengths=(40,)):
    return (
        isinstance(value, str)
        and len(value) in lengths
        and all(char in "0123456789abcdef" for char in value)
    )


@dataclass(frozen=True)
class Applicability:
    key: str
    tags: tuple[str, ...] = ()
    versions: tuple[str, ...] = ()
    expires_at: datetime | None = None

    def __post_init__(self):
        if not isinstance(self.key, str) or not self.key.strip() or len(self.key) > 256:
            raise ValueError("Applicability requires a bounded conflict key")
        if any(not isinstance(tag, str) or not tag.strip() or len(tag) > 128 for tag in self.tags):
            raise ValueError("Invalid exact memory tags")
        if not self.versions or any(not _sha(version) for version in self.versions):
            raise ValueError("Applicability requires explicit commit versions")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("Expiration must have a timezone")
        object.__setattr__(self, "tags", tuple(sorted(set(self.tags))))
        object.__setattr__(self, "versions", tuple(sorted(set(self.versions))))

    def matches(self, version: str, now: datetime | None = None) -> bool:
        return version in self.versions and (
            self.expires_at is None or self.expires_at > (now or datetime.now(UTC))
        )

    def to_dict(self):
        return {
            "key": self.key,
            "tags": list(self.tags),
            "versions": list(self.versions),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass(frozen=True)
class CandidateInput:
    project_id: str
    source_task_id: str
    category: str
    content: str
    evidence: EvidenceRef
    applicability: Applicability
    source_pr: str | None = None
    source_path: str | None = None
    source_blob: str | None = None
    verification_ids: tuple[str, ...] = ()
    sensitivity: str = "public"
    speculative: bool = False

    def __post_init__(self):
        if (
            not self.project_id
            or not self.source_task_id
            or self.category not in {"fact", "design", "cause"}
        ):
            raise ValueError("Candidate requires project, source task and supported category")
        if (
            not isinstance(self.content, str)
            or not self.content.strip()
            or len(self.content) > 8192
        ):
            raise ValueError("Candidate content is missing or exceeds limits")
        if not isinstance(self.evidence, EvidenceRef) or not _sha(self.evidence.sha256, (64,)):
            raise ValueError("Candidate requires immutable evidence")
        if not isinstance(self.applicability, Applicability):
            raise ValueError("Candidate requires explicit applicability")
        if self.sensitivity != "public" or self.speculative is not False:
            raise ValueError("Unknown sensitivity and speculative candidates are blocked")
        if (self.source_blob is None) != (self.source_path is None):
            raise ValueError("A source blob requires its exact path")
        if self.source_blob is not None:
            if not _sha(self.source_blob):
                raise ValueError("Invalid source blob")
            object.__setattr__(self, "source_path", normalize_relative_path(self.source_path))
        if self.source_pr is not None and (
            not isinstance(self.source_pr, str)
            or not self.source_pr.startswith("https://github.com/")
            or "/pull/" not in self.source_pr
            or not self.source_blob
        ):
            raise ValueError("A source PR requires a GitHub PR URL and source blob")
        object.__setattr__(self, "verification_ids", tuple(sorted(set(self.verification_ids))))

    @property
    def fingerprint(self):
        return digest(
            "decagent.memory-candidate.v1",
            {
                "project": self.project_id,
                "task": self.source_task_id,
                "category": self.category,
                "content": self.content.strip(),
                "evidence": self.evidence.sha256,
                "source_pr": self.source_pr,
                "source_path": self.source_path,
                "source_blob": self.source_blob,
                "verification_ids": self.verification_ids,
                "applicability": self.applicability.to_dict(),
            },
        )


@dataclass(frozen=True)
class VerifiedMerge:
    """Observer-owned receipt; observe_merge also checks its durable delivery and evidence.

    Do not deserialize this contract from model tool arguments. A live observer must
    fetch actual GitHub merged content and persist matching app.deliveries first.
    The repaired head commit can differ from merged_sha after squash/rebase.
    """

    project_id: str
    source_task_id: str
    source_pr: str
    merged_sha: str
    blobs: Mapping[str, str | None]
    evidence: EvidenceRef

    def __post_init__(self):
        if not self.project_id or not self.source_task_id or not _sha(self.merged_sha):
            raise ValueError("Merge receipt requires project, source task and actual merge SHA")
        if not isinstance(self.evidence, EvidenceRef):
            raise ValueError("Merge receipt requires evidence")
        normalized = {normalize_relative_path(path): blob for path, blob in self.blobs.items()}
        if not normalized or any(
            blob is not None and not _sha(blob) for blob in normalized.values()
        ):
            raise ValueError("Merge receipt requires actual content blobs")
        object.__setattr__(self, "blobs", MappingProxyType(normalized))


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    project_id: str
    source_task_id: str
    category: str
    content: str
    status: str
    revision: int
    evidence_hash: str
    applicability: Mapping[str, Any]
    user_confirmed: bool = False
    conflicts: tuple[str, ...] = ()
    version_id: str | None = None
    version: int | None = None
    merge_sha: str | None = None
    authority: str = field(default="untrusted_data", init=False)


@dataclass(frozen=True)
class RetrievalRequest:
    project_id: str
    task_id: str
    current_version: str
    task_summary: str
    query: str = ""
    tags: tuple[str, ...] = ()
    limit: int = 20
    owner_selected: tuple[str, ...] = ()
    owner_provenance: Provenance | None = None

    def __post_init__(self):
        if not self.project_id or not self.task_id or not _sha(self.current_version):
            raise ValueError("Retrieval requires project, task and current commit")
        if not isinstance(self.task_summary, str) or not 0 < len(self.task_summary) <= 8192:
            raise ValueError("Bounded task summary is required")
        if not isinstance(self.query, str) or len(self.query) > 1024:
            raise ValueError("Search terms exceed limits")
        if type(self.limit) is not int or not 1 <= self.limit <= 100:
            raise ValueError("Retrieval limit must be between 1 and 100")
        if any(not isinstance(tag, str) or not tag or len(tag) > 128 for tag in self.tags):
            raise ValueError("Invalid exact memory tags")
        if self.owner_selected and (
            not isinstance(self.owner_provenance, Provenance)
            or not self.owner_provenance.trusted_owner
        ):
            raise ValueError("Explicit owner memory selection requires authenticated provenance")
        object.__setattr__(self, "tags", tuple(sorted(set(self.tags))))
        object.__setattr__(self, "owner_selected", tuple(sorted(set(self.owner_selected))))


@dataclass(frozen=True)
class RetrievalResult:
    memories: tuple[MemoryRecord, ...]
    selected: tuple[str, ...]
    excluded: Mapping[str, str]
    degraded: bool
    reason: str


@dataclass(frozen=True)
class ModelOutput:
    value: Any
    tokens: int | None
    cost: Decimal | None


@dataclass(frozen=True)
class ModelCall:
    model: str
    max_tokens: int
    reserved_cost: Decimal | None
    invoke: Callable[[Mapping[str, Any]], ModelOutput]
    allow_unknown_price: bool = False
    usage_hook: Callable[[Mapping[str, Any]], None] | None = None

    def __post_init__(self):
        if not self.model or type(self.max_tokens) is not int or self.max_tokens <= 0:
            raise ValueError("Model calls require an explicit model and positive token bound")
        if not callable(self.invoke):
            raise ValueError("Model client must be injectable")
