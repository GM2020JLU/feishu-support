import subprocess
from types import SimpleNamespace

import pytest
from test_broker_execution_instances import bound

from k3_support.broker_execution_instances import register
from k3_support.broker_systemd_observer import observe_exit, observe_running


@pytest.mark.parametrize("failure", [None, "absent", "exited", "wrong_unit", "duplicate", "missing", "timeout"])
def test_observer_only_registers_exact_manager_running_instance(conn, config, monkeypatch, failure):
    args = bound(conn, config)
    unit = f"k3-support-broker-worker@{args['claim_request_id']}.service"
    fields = {"Id": unit, "LoadState": "loaded", "ActiveState": "active", "SubState": "running",
              "InvocationID": args["invocation_id"], "ControlGroup": args["cgroup_path"], "MainPID": "1234"}
    if failure == "absent":
        fields["LoadState"] = "not-found"
    if failure == "exited":
        fields["SubState"] = "exited"
    if failure == "wrong_unit":
        fields["Id"] = "unrelated.service"
    if failure == "missing":
        del fields["InvocationID"]
    output = "\n".join(f"{key}={value}" for key, value in fields.items())
    if failure == "duplicate":
        output += "\nMainPID=1234"

    def run(argv, **kw):
        assert argv[:3] == ["/usr/bin/systemctl", "--system", "show"]
        assert argv[-1] == unit and "start" not in argv and "stop" not in argv
        assert kw["timeout"] == 5 and kw["stdin"] == subprocess.DEVNULL
        assert kw["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        if failure == "timeout":
            raise subprocess.TimeoutExpired("PRIVATE", 5)
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr("k3_support.broker_systemd_observer.subprocess.run", run)
    before = list(conn.iterdump())
    if failure:
        with pytest.raises(ValueError):
            observe_running(conn, grant_id=args["grant_id"], claim_request_id=args["claim_request_id"])
        assert list(conn.iterdump()) == before
    else:
        assert observe_running(conn, grant_id=args["grant_id"], claim_request_id=args["claim_request_id"])["registered"]
        assert conn.execute("SELECT invocation_id FROM broker_execution_instances").fetchone()[0] == args["invocation_id"]


@pytest.mark.parametrize("mutation", [None, {"InvocationID": "b" * 32}, {"MainPID": "1234"},
                                      {"ExecMainCode": "0"}, {"ExecMainStatus": "999"},
                                      {"ActiveState": "active"}, {"ControlGroup": "/other"}])
def test_exit_requires_previously_bound_exact_instance(conn, config, monkeypatch, mutation):
    args = bound(conn, config)
    register(conn, **args)
    values = {"Id": f"k3-support-broker-worker@{args['claim_request_id']}.service",
              "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
              "InvocationID": args["invocation_id"], "ControlGroup": "", "MainPID": "0",
              "ExecMainPID": "1234", "ExecMainCode": "2", "ExecMainStatus": "15"}
    values.update(mutation or {})
    monkeypatch.setattr("k3_support.broker_systemd_observer.subprocess.run",
                        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="\n".join(f"{k}={v}" for k, v in values.items())))
    before = list(conn.iterdump())
    if mutation:
        with pytest.raises(ValueError):
            observe_exit(conn, grant_id=args["grant_id"])
        assert list(conn.iterdump()) == before
    else:
        result = observe_exit(conn, grant_id=args["grant_id"])
        assert result["service_main_exited"] and not result["descendant_isolation_verified"]
        assert observe_exit(conn, grant_id=args["grant_id"]) == result
        assert conn.execute("SELECT count(*) FROM broker_service_exits").fetchone()[0] == 1
        assert conn.execute("SELECT state FROM jobs").fetchone()[0] == "running"
        assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


@pytest.mark.parametrize('state,code,status', [
    (('active', 'exited'), '1', '0'),
    (('failed', 'failed'), '1', '1'),
    (('failed', 'failed'), '2', '15'),
])
def test_first_observation_after_exit_binds_retained_invocation(conn, config, monkeypatch, state, code, status):
    from k3_support.broker_observer_service import sweep
    from test_broker_completion import add_report

    args = bound(conn, config)
    values = {'Id': f"k3-support-broker-worker@{args['claim_request_id']}.service",
              'LoadState': 'loaded', 'ActiveState': state[0], 'SubState': state[1],
              'InvocationID': args['invocation_id'], 'ControlGroup': '', 'MainPID': '0',
              'ExecMainPID': '1234', 'ExecMainCode': code, 'ExecMainStatus': status,
              'RemainAfterExit': 'yes', 'Slice': 'system.slice'}
    monkeypatch.setattr('k3_support.broker_systemd_observer.subprocess.run',
                        lambda argv, **kw: SimpleNamespace(returncode=0, stdout='\n'.join(
                            f'{k}={values[k]}' for k in argv[4].removeprefix('--property=').split(','))))
    add_report(conn, args['grant_id'])
    assert sweep(conn)['observed'] == 1
    assert conn.execute('SELECT cgroup_path FROM broker_execution_instances').fetchone()[0] == args['cgroup_path']
    # Registration alone does not finish the task; another manager observation
    # must still match the retained invocation before settlement is possible.
    assert conn.execute('SELECT state FROM jobs').fetchone()[0] == 'running'
    assert sweep(conn)['observed'] == 1
    assert conn.execute('SELECT state FROM jobs').fetchone()[0] == ('succeeded' if status == '0' else 'failed')
    assert conn.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0


@pytest.mark.parametrize('mutation', [
    {'InvocationID': ''}, {'InvocationID': '0' * 32}, {'ExecMainPID': '0'},
    {'ExecMainCode': '0'}, {'ExecMainStatus': '256'},
    {'ExecMainCode': '2', 'ExecMainStatus': '0'},
    {'RemainAfterExit': 'no'}, {'Slice': 'other.slice'},
    {'ControlGroup': '/unrelated'}, {'MainPID': '1234'}, {'SubState': 'start'},
])
def test_late_missing_or_ambiguous_evidence_never_registers(conn, config, monkeypatch, mutation):
    from k3_support.broker_systemd_observer import observe_instance

    args = bound(conn, config)
    values = {'Id': f"k3-support-broker-worker@{args['claim_request_id']}.service",
              'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'exited',
              'InvocationID': args['invocation_id'], 'ControlGroup': '', 'MainPID': '0',
              'ExecMainPID': '1234', 'ExecMainCode': '1', 'ExecMainStatus': '0',
              'RemainAfterExit': 'yes', 'Slice': 'system.slice'}
    values.update(mutation)
    monkeypatch.setattr('k3_support.broker_systemd_observer.subprocess.run',
                        lambda *a, **kw: SimpleNamespace(returncode=0, stdout='\n'.join(f'{k}={v}' for k, v in values.items())))
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        observe_instance(conn, grant_id=args['grant_id'], claim_request_id=args['claim_request_id'])
    assert list(conn.iterdump()) == before
