"""Stable pytest collection sharding for parallel CI integration jobs."""

import hashlib

import pytest


def shard_for_nodeid(nodeid, shard_count):
    """Map one pytest node ID to a stable zero-based shard."""
    if type(nodeid) is not str or not nodeid:
        raise ValueError("A nonempty pytest node ID is required")
    if type(shard_count) is not int or shard_count < 1:
        raise ValueError("Shard count must be a positive integer")
    digest = hashlib.sha256(nodeid.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big") % shard_count


def _options(config):
    shard_index = config.getoption("ci_shard_index")
    shard_count = config.getoption("ci_shard_count")
    if shard_index is None and shard_count is None:
        return None
    if shard_index is None or shard_count is None:
        raise pytest.UsageError("Both --ci-shard-index and --ci-shard-count are required")
    if type(shard_count) is not int or shard_count < 1:
        raise pytest.UsageError("--ci-shard-count must be a positive integer")
    if type(shard_index) is not int or not 0 <= shard_index < shard_count:
        raise pytest.UsageError("--ci-shard-index must be within the configured shard range")
    return shard_index, shard_count


def pytest_addoption(parser):
    group = parser.getgroup("ci-shard", "stable integration-test sharding")
    group.addoption("--ci-shard-index", type=int, default=None)
    group.addoption("--ci-shard-count", type=int, default=None)


def pytest_configure(config):
    _options(config)


def pytest_collection_modifyitems(config, items):
    selected_options = _options(config)
    if selected_options is None:
        return
    shard_index, shard_count = selected_options
    selected = []
    deselected = []
    for item in items:
        if shard_for_nodeid(item.nodeid, shard_count) == shard_index:
            target = selected
        else:
            target = deselected
        target.append(item)
    if not selected:
        raise pytest.UsageError("The selected CI shard contains no tests")
    config.hook.pytest_deselected(items=deselected)
    items[:] = selected
