from uuid import uuid4

import pytest

from decagent.executor.broker import match_write
from decagent.executor.protocol import WriteRequest


def test_edit_payload_is_derived_from_exact_governed_parameters():
    request = WriteRequest(
        task_id=str(uuid4()),
        owner="worker",
        lease_generation=1,
        action_id=str(uuid4()),
        action_digest="a" * 64,
        path="x.py",
        content_base64="eA==",
    )
    snapshot = {
        "tool": "edit_file",
        "params": {"path": "x.py", "old": "10", "new": "9", "expected_matches": 1},
    }
    match_write(request, snapshot, b"x=9\n", current=b"x=10\n")
    with pytest.raises(ValueError, match="governed"):
        match_write(request, snapshot, b"unrelated", current=b"x=10\n")
    with pytest.raises(ValueError, match="governed"):
        match_write(request, snapshot, b"x=9\ny=9\n", current=b"x=10\ny=10\n")
