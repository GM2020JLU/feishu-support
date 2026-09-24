import pytest

from k3_support.codex_remote import RemoteSandboxError, run_remote


def test_missing_control_database_never_created(config, monkeypatch):
    path = config.database_path
    assert not path.exists()
    monkeypatch.setattr("k3_support.codex_remote.subprocess.Popen", lambda *a, **kw: pytest.fail("must not start SSH"))
    with pytest.raises(RemoteSandboxError):
        run_remote(config, case_id="K3-synthetic", mode="inspect", repo_name=None, command="true")
    assert not path.exists()


def test_unauthorized_remote_check_does_not_mutate_database(conn, config, monkeypatch):
    before = list(conn.iterdump())
    monkeypatch.setattr("k3_support.codex_remote.subprocess.Popen", lambda *a, **kw: pytest.fail("must not start SSH"))
    with pytest.raises(RemoteSandboxError):
        run_remote(config, case_id="K3-missing", mode="inspect", repo_name=None, command="true")
    assert list(conn.iterdump()) == before
