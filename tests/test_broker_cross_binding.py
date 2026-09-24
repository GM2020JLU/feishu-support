from datetime import UTC, datetime

import pytest
from test_broker_receipts import setup
from test_review import active_config

from k3_support.broker_renew import renew
from k3_support.broker_results import submit
from k3_support.broker_start import authorize
from k3_support.executors import CODEX_RESULT_SECTIONS
from k3_support.store import create_case


@pytest.mark.parametrize("method", ["start", "renew", "result"])
@pytest.mark.parametrize("field,value", [
    ("case_id", "another-case"), ("job_id", "another-job"),
    ("execution_round", 2), ("lifecycle_round", 2), ("input_digest", "b" * 64),
])
def test_cross_bound_writes_leave_database_unchanged(conn, config, method, field, value):
    request = setup(conn)
    if field == "case_id":
        value, _ = create_case(conn, title="other synthetic case", case_type="bug",
                               severity="P3", confidence=0.8)
    request["method"] = method
    request["params"][field] = value
    if method == "result":
        request["params"]["result"] = "\n".join(
            f"## {name}\n" + ("completed" if name == "status" else "not verified")
            for name in CODEX_RESULT_SECTIONS)
    args = {"peer_uid": 1234, "now": datetime(2026, 9, 8, 0, 30, tzinfo=UTC)}
    cfg = active_config(config)
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        if method == "start":
            authorize(conn, cfg, request, **args)
        elif method == "renew":
            renew(conn, request, config=cfg, **args)
        else:
            submit(conn, request, **args)
    assert list(conn.iterdump()) == before
