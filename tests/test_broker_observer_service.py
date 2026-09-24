import json
import os
import threading
from types import SimpleNamespace

from test_broker_completion import add_report
from test_broker_execution_instances import bound

from k3_support.broker_observer_service import sweep


def test_dispatch_service_opt_in_uses_protected_contract_and_durable_intent(conn, config, tmp_path, monkeypatch):
    import yaml
    from test_broker_claim import queued
    from test_broker_execution_contract import contract
    from test_review import active_config

    from k3_support.broker_observer_service import run

    queued(conn)
    conn.execute("UPDATE jobs SET available_at='2000-01-01T00:00:00+00:00'")
    cfg = active_config(config)
    cfg.path.write_text(yaml.safe_dump(cfg.raw))
    directory = tmp_path / "contract"
    directory.mkdir(mode=0o755)
    (directory / "execution-contract.json").write_text(json.dumps(contract()))
    launched = []
    def launch(path, request_id):
        assert path == "/synthetic/launcher.sock"
        assert conn.execute("SELECT state FROM broker_launches WHERE claim_request_id=?", (request_id,)).fetchone()[0] == "launching"
        launched.append(request_id)
        return True
    monkeypatch.setattr("k3_support.broker_launcher.request", launch)
    result = run(config_path=cfg.path, watch=False, stop_event=threading.Event())
    assert "dispatch" not in result and launched == []
    result = run(config_path=cfg.path, watch=False, stop_event=threading.Event(), dispatch_workers=True,
                 contract_directory=directory, worker_uid=os.geteuid()+1, launcher_socket="/synthetic/launcher.sock")
    assert result["dispatch"]["state"] == "accepted" and len(launched) == 1
    result = run(config_path=cfg.path, watch=False, stop_event=threading.Event(), dispatch_workers=True,
                 contract_directory=directory, worker_uid=os.geteuid()+1, launcher_socket="/synthetic/launcher.sock")
    assert result["dispatch"]["state"] == "occupied" and len(launched) == 1


def test_sweep_runs_actual_registration_and_exit_collectors(conn, config, monkeypatch, capsys):
    args = bound(conn, config)
    values = {"Id": f"k3-support-broker-worker@{args['claim_request_id']}.service",
              "LoadState": "loaded", "ActiveState": "active", "SubState": "running",
              "InvocationID": args["invocation_id"], "ControlGroup": args["cgroup_path"], "MainPID": "1234",
              "ExecMainPID": "1234", "ExecMainCode": "0", "ExecMainStatus": "0",
              "RemainAfterExit": "yes", "Slice": "system.slice"}
    monkeypatch.setattr("k3_support.broker_systemd_observer.subprocess.run",
                        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="\n".join(
                            f"{k}={values[k]}" for k in argv[4].removeprefix("--property=").split(","))))
    first = sweep(conn)
    assert first["observed"] == 1
    assert conn.execute("SELECT count(*) FROM broker_execution_instances").fetchone()[0] == 1
    assert sweep(conn, after=first["after"])["after"] == ""
    values.update(ActiveState="inactive", SubState="dead", MainPID="0", ControlGroup="",
                  ExecMainPID="1234", ExecMainCode="1", ExecMainStatus="0")
    add_report(conn, args["grant_id"])
    assert sweep(conn)["observed"] == 1
    assert conn.execute("SELECT count(*) FROM broker_service_exits").fetchone()[0] == 1
    assert sweep(conn) == {"observed": 0, "unverified": 0, "after": ""}
    assert conn.execute("SELECT state FROM jobs").fetchone()[0] == "succeeded"
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert capsys.readouterr().out == ""


def test_failed_observation_advances_cursor_without_fabricating_exit(conn, config, monkeypatch):
    args = bound(conn, config)

    def unavailable(*a, **kw):
        raise ValueError("private manager diagnostic")

    monkeypatch.setattr("k3_support.broker_observer_service.observe_instance", unavailable)
    before = list(conn.iterdump())
    result = sweep(conn)
    assert result == {"observed": 0, "unverified": 1, "after": args["grant_id"]}
    assert list(conn.iterdump()) == before


def test_shutdown_does_not_start_another_observation(conn, config, monkeypatch):
    bound(conn, config)
    stopped = threading.Event()
    stopped.set()
    monkeypatch.setattr("k3_support.broker_observer_service.observe_instance",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("unexpected observation")))
    assert sweep(conn, stop_event=stopped) == {"observed": 0, "unverified": 0, "after": ""}
