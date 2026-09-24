import pytest
from test_broker_execution_instances import bound

from k3_support.broker_execution_instances import register
from k3_support.broker_unit_references import UnitReferences


def fixture_refs(monkeypatch):
    refs = UnitReferences.__new__(UnitReferences)
    refs.units = {}
    calls = []
    refs._call = lambda method, unit: calls.append((method, unit))
    return refs, calls


def test_reference_precedes_observation_and_release_waits_for_exit_receipt(conn, config, monkeypatch):
    args = bound(conn, config)
    refs, calls = fixture_refs(monkeypatch)

    def observe(db, **kwargs):
        assert calls[0][0] == "RefUnit"
        return register(db, **args)

    monkeypatch.setattr("k3_support.broker_unit_references.observe_running", observe)
    refs.observe(conn, grant_id=args["grant_id"], claim_request_id=args["claim_request_id"])
    refs.release_recorded(conn)
    assert len(calls) == 1 and len(refs.units) == 1
    conn.execute("INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)",
                 (args["grant_id"], args["invocation_id"], 1234, 1, 0, "synthetic"))
    refs.release_recorded(conn)
    assert calls[-1][0] == "UnrefUnit" and not refs.units


def test_failed_observation_keeps_reference_for_reconciliation(conn, config, monkeypatch):
    args = bound(conn, config)
    refs, calls = fixture_refs(monkeypatch)

    def unavailable(*a, **kw):
        raise ValueError("unavailable")

    monkeypatch.setattr("k3_support.broker_unit_references.observe_running", unavailable)
    with pytest.raises(ValueError):
        refs.observe(conn, grant_id=args["grant_id"], claim_request_id=args["claim_request_id"])
    assert len(refs.units) == 1 and len(calls) == 1
    with pytest.raises(ValueError, match="already retained"):
        refs.observe(conn, grant_id=args["grant_id"], claim_request_id=args["claim_request_id"])
    assert len(calls) == 1


def test_retained_units_do_not_require_privileged_bus_reference(monkeypatch):
    from k3_support import broker_unit_references as module
    seen = []
    monkeypatch.setattr(module, 'observe_running', lambda conn, **kw: seen.append(kw) or {'observed': True})
    monkeypatch.setattr(module.ctypes, 'CDLL', lambda *_: (_ for _ in ()).throw(AssertionError('no bus connection')))
    observer = module.RetainedUnitObservations()
    assert observer.observe(None, grant_id='grant', claim_request_id='claim') == {'observed': True}
    assert seen == [{'grant_id': 'grant', 'claim_request_id': 'claim'}]
    assert observer.release_recorded(None) == 0
    observer.close()
