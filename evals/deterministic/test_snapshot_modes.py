from decagent.executor.archive import make_archive, read_archive, snapshot_hash
from decagent.integrations.delivery import Publication


def test_executable_bit_survives_snapshot_and_changes_verification_hash():
    from decagent.executor.archive import FilesSnapshot

    before = FilesSnapshot({"entry.py": b"print(1)"}, modes={"entry.py": 0o755})
    copied = read_archive(make_archive(before))
    assert copied.modes["entry.py"] == 0o755
    assert snapshot_hash(copied) != snapshot_hash(dict(copied))
    after = FilesSnapshot({"entry.py": b"print(2)"}, modes={"entry.py": 0o755})
    plan = Publication.from_snapshots(
        "9bbcfb39-5685-42d9-ae2e-1d83eb138091", "a" * 40, before, after, issue="Bug", behavior="Fix"
    )
    assert plan.changes[0].executable is True


def test_mode_only_change_is_in_the_published_delta():
    from decagent.executor.archive import FilesSnapshot

    before = FilesSnapshot({"entry.py": b"print(1)"}, modes={"entry.py": 0o755})
    after = FilesSnapshot({"entry.py": b"print(1)"}, modes={"entry.py": 0o644})
    plan = Publication.from_snapshots(
        "9bbcfb39-5685-42d9-ae2e-1d83eb138091", "a" * 40, before, after, issue="Bug", behavior="Fix"
    )
    assert len(plan.changes) == 1 and plan.changes[0].executable is False
