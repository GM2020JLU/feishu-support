import hashlib

import pytest

from k3_support.operations import OperationsError
from k3_support.recovery_inventory import audit
from k3_support.store import ingest_event


def seed(conn, path, key="source"):
    ingest_event(conn, source="feishu_bot_im", identity="bot", external_id=key,
                 payload={}, raw_artifact_path=str(path), occurred_at="2026-09-08T00:00:00+00:00")


def test_audit_hashes_referenced_file_without_writing(config, conn):
    path = config.data_dir / "cases" / "fixture.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"private fixture body")
    seed(conn, path)
    changes = conn.total_changes
    result = audit(conn, config)
    assert result["complete"] and not result["workflow_recoverable"]
    assert result["items"][0]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["items"][0]["relative_path"] == "cases/fixture.txt"
    assert "private fixture body" not in str(result)
    assert conn.total_changes == changes


@pytest.mark.parametrize("kind", ["missing", "link", "parent_link", "outside", "oversize"])
def test_incomplete_files_never_report_recoverable(config, conn, tmp_path, kind):
    directory = config.data_dir / "attachments"
    directory.mkdir(parents=True)
    path = directory / "fixture"
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    if kind == "link":
        path.symlink_to(outside)
    elif kind == "parent_link":
        (directory / "linked").symlink_to(tmp_path, target_is_directory=True)
        path = directory / "linked" / "outside"
    elif kind == "outside":
        path = outside
    elif kind == "oversize":
        path.write_bytes(b"too large")
    seed(conn, path)
    result = audit(conn, config, max_bytes=4)
    assert not result["complete"] and not result["workflow_recoverable"]
    assert "sha256" not in result["items"][0]


def test_audit_limit_cannot_claim_full_coverage(config, conn):
    for number in range(2):
        seed(conn, config.data_dir / "cases" / str(number), key=str(number))
    result = audit(conn, config, max_files=1)
    assert result["truncated"] and not result["complete"]
    assert len(result["items"]) == 1
    with pytest.raises(OperationsError):
        audit(conn, config, max_files=True)


def test_changed_source_binding_invalidates_coverage(config, conn, monkeypatch):
    import k3_support.recovery_inventory as inventory

    path = config.data_dir / "cases" / "fixture"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fixture")
    seed(conn, path)
    original = inventory._hash

    def change_binding(path, limit):
        result = original(path, limit)
        conn.execute("UPDATE inbound_events SET raw_artifact_path=NULL")
        return result

    monkeypatch.setattr(inventory, "_hash", change_binding)
    result = audit(conn, config)
    assert result["references_changed"] and not result["complete"]
