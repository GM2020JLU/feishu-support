import pytest
from test_retention_recheck import candidate

from k3_support.operations import OperationsError, apply_retention
from k3_support.retention_recovery import recover, recovery_preview


@pytest.mark.parametrize("fail", [False, True])
def test_recovery_audit_intent_is_bound_and_unknown_never_retries(conn, config, monkeypatch, fail):
    from uuid import uuid4

    from k3_support.retention_recovery import apply_recovery
    path, _, preview = candidate(conn, config)
    apply_retention(conn, config, preview)
    attempt = conn.execute("SELECT attempt_id FROM retention_attempts").fetchone()[0]
    shown = recovery_preview(conn, attempt)
    args = {"attempt_id": attempt, "binding_digest": shown["binding_digest"], "request_id": str(uuid4()), "actor_id": "owner"}
    if fail:
        def broken(*a, **kw):
            assert conn.execute("SELECT state FROM retention_recovery_requests").fetchone()[0] == "running"
            raise OSError("interrupted")
        monkeypatch.setattr("k3_support.retention_recovery.recover", broken)
        with pytest.raises(OSError):
            apply_recovery(conn, config, **args)
    else:
        assert apply_recovery(conn, config, **args)["state"] == "restored"
    assert apply_recovery(conn, config, **args)["state"] == ("unknown" if fail else "restored")
    with pytest.raises(ValueError, match="binding"):
        apply_recovery(conn, config, **{**args, "actor_id": "other"})
    row = conn.execute("SELECT actor_id,state FROM retention_recovery_requests").fetchone()
    assert tuple(row) == ("owner", "unknown" if fail else "restored")
    assert path.exists() is (not fail)


@pytest.mark.parametrize("changed", [False, True])
def test_check_recovers_post_restore_crash_without_moving_files(conn, config, monkeypatch, changed):
    from uuid import uuid4

    from k3_support import retention_recovery as module
    path, _, preview = candidate(conn, config)
    apply_retention(conn, config, preview)
    attempt = conn.execute("SELECT attempt_id FROM retention_attempts").fetchone()[0]
    shown = recovery_preview(conn, attempt)
    ident = str(uuid4())
    original = module.recover
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("post-restore interruption")
    monkeypatch.setattr(module, "recover", interrupted)
    with pytest.raises(OSError):
        module.apply_recovery(conn, config, attempt_id=attempt, binding_digest=shown["binding_digest"], request_id=ident, actor_id="owner")
    if changed:
        path.write_text("new user content")
    before = path.stat()
    with pytest.raises(ValueError):
        module.check_recovery(conn, config, request_id=ident, actor_id="other")
    result = module.check_recovery(conn, config, request_id=ident, actor_id="owner")
    assert result["state"] == ("unknown" if changed else "restored")
    after = path.stat()
    assert (before.st_ino, before.st_mtime_ns, before.st_size) == (after.st_ino, after.st_mtime_ns, after.st_size)


@pytest.mark.parametrize("change", [None, "state", "binding", "occupied"])
def test_confirmed_recovery_is_bound_and_never_overwrites_new_file(conn, config, change):
    path, event, preview = candidate(conn, config)
    assert apply_retention(conn, config, preview)["quarantined"] == 1
    attempt = conn.execute("SELECT attempt_id FROM retention_attempts").fetchone()[0]
    before = list(conn.iterdump())
    shown = recovery_preview(conn, attempt)
    assert list(conn.iterdump()) == before and not path.exists()
    assert str(path) not in str(shown)
    if change == "state":
        conn.execute("UPDATE retention_attempts SET state='failed'")
    elif change == "binding":
        conn.execute("UPDATE inbound_events SET raw_artifact_path='other' WHERE event_pk=?", (event,))
    elif change == "occupied":
        path.write_text("new owner content")
    if change:
        with pytest.raises(OperationsError):
            recover(conn, config, attempt, expected_digest=shown["binding_digest"])
        assert path.read_text() == "new owner content" if change == "occupied" else not path.exists()
    else:
        assert recover(conn, config, attempt, expected_digest=shown["binding_digest"])["state"] == "restored"
        assert path.read_text() == "retained evidence"
        with pytest.raises(OperationsError, match="失效"):
            recover(conn, config, attempt, expected_digest=shown["binding_digest"])
