"""Evidence-backed fixed materials. The writable repository cannot replace these records."""

from pathlib import Path

from decagent.storage.evidence import EvidenceRef, EvidenceStore

from .models import ReproductionBundle


class BundleStore:
    def __init__(self, evidence: EvidenceStore, *, workspace_root: Path):
        if evidence.root.is_relative_to(workspace_root.resolve()):
            raise ValueError("Repair evidence must be outside the writable workspace")
        self.evidence = evidence

    def save(self, bundle: ReproductionBundle) -> EvidenceRef:
        source = bundle.model_dump_json()
        reference = self.evidence.write(source, "repair:reproduction-bundle")
        if self.evidence.read(reference) != source:
            raise ValueError("Sensitive fixed material cannot be frozen after redaction")
        return reference

    def load(self, reference: EvidenceRef) -> ReproductionBundle:
        return ReproductionBundle.model_validate_json(self.evidence.read(reference))
