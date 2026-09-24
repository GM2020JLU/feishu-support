import pytest

from k3_support.audit_inventory import page


def populate(conn, count=35):
    for i in range(count):
        conn.execute(
            "INSERT INTO global_control_events VALUES(?, 'global', ?, 'observe', 'collaborate', 0, 1, 'owner', 'gui', 'PRIVATE REASON', '2026-09-07T00:00:00+00:00')",
            (f"event-{i}", f"external-{i}"),
        )


def test_all_records_paginate_without_duplicate_and_hide_private_fields(conn):
    populate(conn)
    before = list(conn.iterdump())
    seen = []
    cursor = None
    while True:
        result = page(conn, cursor=cursor, limit=7)
        assert result["total_matching"] == 35 and result["read_only"]
        assert "PRIVATE" not in str(result) and "external-" not in str(result)
        seen.extend(item["key"] for item in result["items"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 35
    assert list(conn.iterdump()) == before


def test_category_bound_cursor_and_timezone_order(conn):
    populate(conn, 2)
    conn.execute(
        "UPDATE global_control_events SET created_at='2026-09-07T09:00:00+08:00' WHERE event_id='event-0'"
    )
    result = page(conn, kind="mode", limit=1)
    assert result["items"][0]["key"] == "mode:event-0"
    with pytest.raises(ValueError, match="cursor"):
        page(conn, kind="budget", cursor=result["next_cursor"])
    assert not page(conn, kind="budget")["items"]


def test_config_history_projection_does_not_export_raw_json(conn):
    conn.execute(
        "INSERT INTO feature_settings_history VALUES(1,'digest','{\"SECRET\":true}','{}','draft','owner','2026-09-07T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO model_budget_policy_history VALUES(1,'{\"SECRET\":true}','owner','2026-09-07T00:00:00+00:00')"
    )
    result = page(conn)
    assert result["total_matching"] == 2
    assert {item["kind"] for item in result["items"]} == {"features", "budget"}
    assert "SECRET" not in str(result) and "draft" not in str(result)
    assert page(conn, kind="budget")["total_matching"] == 1


def test_malformed_legacy_time_does_not_break_or_lose_pagination(conn):
    populate(conn, 3)
    conn.execute("UPDATE global_control_events SET created_at='unknown'")
    first = page(conn, limit=1)
    second = page(conn, limit=2, cursor=first["next_cursor"])
    assert len(first["items"]) + len(second["items"]) == 3


def test_work_hours_all_revisions_paginate_readonly_without_raw_values(conn):
    for revision in range(1, 36):
        conn.execute(
            "INSERT INTO work_hours_history VALUES(?, ?, ?, 'owner', '2026-09-07T00:00:00+00:00')",
            (revision, '{"private":"SECRET"}', '{"start":"09:00","end":"18:00"}'),
        )
    before = list(conn.iterdump())
    items, cursor = [], None
    while True:
        result = page(conn, kind="work_hours", cursor=cursor, limit=7)
        assert result["total_matching"] == 35
        assert "SECRET" not in str(result) and "09:00" not in str(result)
        items.extend(result["items"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert [item["summary"] for item in items] == [
        f"工作时间版本 {revision}" for revision in range(35, 0, -1)
    ]
    assert all(item["actor_id"] == "owner" for item in items)
    assert page(conn)["total_matching"] == 35
    assert list(conn.iterdump()) == before


def test_case_and_approval_records_never_export_payload_or_invent_actor(conn, config):
    from test_executors import approved_board, executor_config

    from k3_support.store import create_case

    case, _ = create_case(
        conn, title="PRIVATE TITLE", case_type="bug", severity="P2", confidence=0.2
    )
    approval = approved_board(conn, executor_config(config, board=True), case)
    conn.execute(
        'UPDATE case_events SET detail_json=\'{"private":"SECRET"}\' WHERE case_id=?',
        (case,),
    )
    conn.execute(
        "UPDATE approvals SET decision_text='SECRET',approval_message_id='SECRET' WHERE approval_id=?",
        (approval,),
    )
    before = list(conn.iterdump())
    events = page(conn, kind="case")
    assert events["items"] and all(r["target"] == case for r in events["items"])
    approvals = page(conn, kind="approval")
    item = approvals["items"][0]
    assert item["target"] == approval
    assert "当前快照" in item["summary"]
    assert item["actor_id"] == "状态更新人未记录"
    assert "SECRET" not in str(events) + str(approvals)
    assert "PRIVATE TITLE" not in str(events)
    assert list(conn.iterdump()) == before


def test_delivery_history_preserves_unknown_and_late_receipt_without_exposing_tokens(
    conn,
):
    from test_delivery_attempts import enqueue

    from k3_support.delivery import claim_outbox

    enqueue(conn)
    claimed = claim_outbox(conn, worker_id="fixture-worker")
    token = claimed["claim_token"]
    conn.execute(
        "UPDATE outbox SET destination='SECRET DESTINATION',payload_json='{\"text\":\"SECRET BODY\"}'"
    )
    conn.execute(
        "UPDATE outbox_attempts SET dispatch_started_at='2026-09-07T00:00:00+00:00' WHERE claim_token=?",
        (token,),
    )
    for name, stamp in [
        ("uncertain", "2026-09-07T00:00:01+00:00"),
        ("delivered", "2026-09-07T00:00:02+00:00"),
    ]:
        conn.execute(
            "INSERT INTO outbox_attempt_events VALUES(?,?,?,'SECRET REMOTE','{\"private\":\"SECRET DETAIL\"}',?)",
            (f"event-{name}", token, name, stamp),
        )
    before = list(conn.iterdump())
    result = page(conn, kind="delivery")
    assert result["total_matching"] == 5
    summaries = " ".join(item["summary"] for item in result["items"])
    assert "uncertain" in summaries and "delivered" in summaries
    assert "不代表对方已读" in summaries and "尚不证明派发" in summaries
    assert token not in str(result) and "SECRET" not in str(result)
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": []},
        {"kind": "secret"},
        {"cursor": "bad"},
        {"cursor": []},
        {"limit": True},
    ],
)
def test_invalid_audit_request(conn, kwargs):
    with pytest.raises(ValueError):
        page(conn, **kwargs)
