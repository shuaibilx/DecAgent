"""Stable references for employee memory in published business workflows."""

from decagent.business.mcp_contract import WRITE_COMMANDS

PREFERENCE_SUBJECTS = frozenset(
    {
        "response_language",
        "response_style",
        "ticket_category_preference",
        "communication_preference",
    }
)
MEMORY_KINDS = frozenset({"confirmed_preference", "verified_workflow_experience"})


class MemorySlotAmbiguous(ValueError):
    """More than one current employee memory resolves to the same stable slot."""


def validate_memory_slot(memory_kind, subject):
    """Return whether a workflow slot names a registered memory subject."""
    if not isinstance(memory_kind, str) or not isinstance(subject, str):
        return False
    if memory_kind == "confirmed_preference":
        return subject in PREFERENCE_SUBJECTS
    if memory_kind == "verified_workflow_experience":
        command = subject.removeprefix("workflow:")
        return subject.startswith("workflow:") and command in WRITE_COMMANDS
    return False


def memory_slot_key(memory_kind, subject):
    """Build the collision-free runtime key for an explicitly declared slot."""
    if not validate_memory_slot(memory_kind, subject):
        raise ValueError("Unregistered stable memory slot")
    return memory_kind + ":" + subject


def memory_slot_from_binding(binding):
    """Return the (kind, subject) tuple declared by a memory binding."""
    memory_kind = binding["memory_kind"]
    subject = binding["subject"]
    if not validate_memory_slot(memory_kind, subject):
        raise ValueError("Unregistered stable memory slot")
    return memory_kind, subject
