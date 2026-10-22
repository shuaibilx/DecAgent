from collections import Counter

import pytest

from ops.testing.pytest_ci_shard import shard_for_nodeid


@pytest.mark.parametrize("shard_count", [2, 3, 4, 7])
def test_nodeids_have_one_stable_shard_and_every_shard_receives_cases(shard_count):
    nodeids = [
        f"evals/integration/test_case_{index}.py::test_case[{index}]"
        for index in range(257)
    ]

    assignments = [shard_for_nodeid(nodeid, shard_count) for nodeid in nodeids]
    repeated = [shard_for_nodeid(nodeid, shard_count) for nodeid in nodeids]

    assert assignments == repeated
    assert all(0 <= shard < shard_count for shard in assignments)
    assert set(assignments) == set(range(shard_count))
    assert sum(Counter(assignments).values()) == len(nodeids)


@pytest.mark.parametrize(
    ("nodeid", "shard_count"),
    [("", 2), ("case", 0), ("case", -1), ("case", True), (None, 2)],
)
def test_shard_assignment_rejects_invalid_inputs(nodeid, shard_count):
    with pytest.raises(ValueError):
        shard_for_nodeid(nodeid, shard_count)
