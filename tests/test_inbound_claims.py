from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from k3_support.config import Config, validate_config
from k3_support.db import connect, transaction
from k3_support.inbound_claims import (
    FencedConnection,
    InboundClaimLost,
    InboundHeartbeat,
    fail_claim,
    publish_worker_health,
    renew_inbound_claim,
    start_processing,
)
from k3_support.knowledge import create_candidate, review
from k3_support.orchestrator import claim_inbound, process_inbound
from k3_support.runtime_control import ensure_global_state
from k3_support.store import ingest_event


def _event(conn, external_id="om_lease"):
    return ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id=external_id,
        payload={"content": "K3 U-Boot 如何启动", "chat_type": "p2p"},
        occurred_at=datetime.now(UTC).isoformat(),
        sender_id="ou_peer",
        chat_id="oc_peer",
    )[0]


def _config(config):
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["features"]["auto_faq"] = True
    raw["identity"]["feishu_owner_open_id"] = "ou_owner"
    return Config(validate_config(raw), config.path)


def _knowledge(conn):
    knowledge_id = create_candidate(
        conn,
        title="K3 U-Boot 启动",
        questions=["K3 U-Boot 如何启动"],
        answer_markdown="请参考审核过的启动指南。",
        project="k3",
        module="boot",
        software_version=None,
        disclosure_class="public",  # Synthetic lease fixture, not an ACL test.
        confidence=0.99,
        source_authority=0.99,
        canonical_case_id=None,
        source_digest="source-test",
    )
    review(
        conn, knowledge_id=knowledge_id, reviewer_id="owner-user", decision="approved"
    )
    from k3_support.knowledge_corpus import build
    assert build(conn)['built']
    return knowledge_id


def _selection(knowledge_id):
    return {"knowledge_id": knowledge_id, "confidence": 0.99}


def _expire(conn, event_pk):
    conn.execute(
        "UPDATE inbound_events SET lease_expires_at=? WHERE event_pk=?",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), str(event_pk)),
    )


def _guard(conn, event_pk, *, stop_requested=lambda: False):
    claim, _ = start_processing(
        conn,
        event_pk=event_pk,
        worker_id="worker:test-instance",
        expected_token=None,
        lease_seconds=120,
    )
    monitor = InboundHeartbeat(
        None,
        claim,
        lease_seconds=120,
        interval_seconds=30,
        stop_requested=stop_requested,
    )
    return FencedConnection(conn, claim, monitor), claim


def test_claim_handles_are_unique_and_plain_ids_cannot_adopt_successor(conn):
    event_pk = _event(conn)
    first = claim_inbound(conn, worker_id="same-worker")[0]
    assert first == event_pk
    assert (
        process_inbound(conn, event_pk=str(first), worker_id="same-worker")["reason"]
        == "claim_token_required"
    )
    _expire(conn, event_pk)
    second = claim_inbound(conn, worker_id="same-worker")[0]
    assert second.claim_token != first.claim_token
    with pytest.raises(InboundClaimLost, match="superseded"):
        process_inbound(conn, event_pk=first, worker_id="same-worker")
    assert (
        process_inbound(
            conn, event_pk=json.loads(json.dumps(second)), worker_id="same-worker"
        )["reason"]
        == "claim_token_required"
    )
    result = process_inbound(conn, event_pk=second, worker_id="same-worker")
    assert result["processed"]
    assert conn.execute("SELECT attempt_count FROM inbound_events").fetchone()[0] == 2


@pytest.mark.parametrize("late_error", [False, True])
def test_slow_selector_reclaim_same_worker_late_result_cannot_write(
    conn, config, late_error
):
    cfg = _config(config)
    knowledge_id = _knowledge(conn)
    event_pk = _event(conn)
    entered, release = threading.Event(), threading.Event()
    outcomes = []

    def slow_selector(query, catalog):
        entered.set()
        assert release.wait(5)
        if late_error:
            raise ValueError("late model failure")
        return _selection(knowledge_id)

    def old_worker():
        own_conn = connect(cfg.database_path)
        try:
            process_inbound(
                own_conn,
                event_pk=event_pk,
                worker_id="same-worker",
                config=cfg,
                semantic_selector=slow_selector,
                heartbeat_interval_seconds=0.03,
            )
        except Exception as exc:  # noqa: BLE001 - expose any child-thread failure to the deterministic test assertion
            outcomes.append(exc)
        finally:
            own_conn.close()

    worker = threading.Thread(target=old_worker)
    worker.start()
    try:
        assert entered.wait(5)
        old_token = conn.execute("SELECT claim_token FROM inbound_events").fetchone()[0]
        _expire(conn, event_pk)
        replacement = claim_inbound(conn, worker_id="same-worker")[0]
        assert replacement.claim_token != old_token
        successor = process_inbound(
            conn,
            event_pk=replacement,
            worker_id="same-worker",
            config=cfg,
            semantic_selector=lambda *_: _selection(knowledge_id),
        )
        snapshot = dict(conn.execute("SELECT * FROM inbound_events").fetchone())
        counts = tuple(
            conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "cases",
                "route_decisions",
                "outbox",
                "case_suggestions",
                "jobs",
            )
        )
        release.set()
        worker.join(6)
        assert not worker.is_alive()
        assert len(outcomes) == 1
        # The post-callback fence also runs when the stale model raises: lease
        # supersession takes precedence over its irrelevant late failure.
        assert isinstance(outcomes[0], InboundClaimLost)
        assert dict(conn.execute("SELECT * FROM inbound_events").fetchone()) == snapshot
        assert (
            tuple(
                conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "cases",
                    "route_decisions",
                    "outbox",
                    "case_suggestions",
                    "jobs",
                )
            )
            == counts
        )
        assert successor["processed"]
        assert counts[0:3] == (1, 1, 1)
        assert conn.execute("SELECT action_type FROM outbox").fetchone()[0] == "reply"
    finally:
        release.set()
        worker.join(6)


def test_independent_connection_renews_during_blocked_selector(conn, config):
    cfg = _config(config)
    knowledge_id = _knowledge(conn)
    event_pk = _event(conn)
    entered, release = threading.Event(), threading.Event()
    outcome = []

    def selector(*_):
        entered.set()
        assert release.wait(5)
        return _selection(knowledge_id)

    def run():
        own_conn = connect(cfg.database_path)
        try:
            outcome.append(
                process_inbound(
                    own_conn,
                    event_pk=event_pk,
                    worker_id="renewing-instance",
                    config=cfg,
                    semantic_selector=selector,
                    lease_seconds=0.3,
                    heartbeat_interval_seconds=0.02,
                )
            )
        except Exception as exc:  # noqa: BLE001 - capture every background failure instead of losing it in a thread
            outcome.append(exc)
        finally:
            own_conn.close()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(5)
        time.sleep(
            0.65
        )  # More than two original leases; renewal owns another connection.
        row = conn.execute("SELECT * FROM inbound_events").fetchone()
        assert datetime.fromisoformat(row["lease_expires_at"]) > datetime.now(UTC)
        assert row["heartbeat_at"] > row["processing_started_at"]
        assert claim_inbound(conn, worker_id="challenger") == []
        with pytest.raises(InboundClaimLost, match="already started"):
            process_inbound(
                conn,
                event_pk=event_pk,
                worker_id="renewing-instance",
                expected_claim_token=row["claim_token"],
            )
        release.set()
        worker.join(6)
        assert not worker.is_alive()
        assert len(outcome) == 1 and isinstance(outcome[0], dict)
        assert outcome[0]["processed"]
        assert (
            conn.execute("SELECT attempt_count FROM inbound_events").fetchone()[0] == 1
        )
    finally:
        release.set()
        worker.join(6)


@pytest.mark.parametrize("stop_kind", ["process", "paused", "stopped", "pause_resume"])
def test_slow_model_stop_blocks_all_downstream_writes(conn, config, stop_kind):
    cfg = _config(config)
    knowledge_id = _knowledge(conn)
    event_pk = _event(conn)
    stop = threading.Event()
    if stop_kind != "process":
        ensure_global_state(conn)
    second = connect(cfg.database_path)
    changed_at = []

    def selector(*_):
        assert not conn.in_transaction
        if stop_kind == "process":
            stop.set()
        else:
            second.execute(
                "UPDATE global_control_state SET mode=?,revision=revision+1",
                ("paused" if stop_kind == "pause_resume" else stop_kind,),
            )
            if stop_kind == "pause_resume":
                second.execute(
                    "UPDATE global_control_state SET mode='auto',revision=revision+1"
                )
        changed_at.append(conn.total_changes)
        return _selection(knowledge_id)

    try:
        with pytest.raises(InboundClaimLost):
            process_inbound(
                conn,
                event_pk=event_pk,
                worker_id="stoppable-instance",
                config=cfg,
                semantic_selector=selector,
                stop_requested=stop.is_set,
            )
        assert conn.total_changes == changed_at[0]
        for table in (
            "cases",
            "requester_profiles",
            "route_decisions",
            "outbox",
            "jobs",
        ):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    finally:
        second.close()


@pytest.mark.parametrize(
    "boundary",
    [
        "record_route_decision",
        "create_case",
        "enqueue_outbox",
        "create_retrieval_job",
    ],
)
def test_each_downstream_entry_is_fenced_after_selector(
    conn, config, monkeypatch, boundary
):
    from k3_support import orchestrator

    cfg = _config(config)
    knowledge_id = _knowledge(conn)
    event_pk = _event(conn)
    other = connect(cfg.database_path)
    original = getattr(orchestrator, boundary)
    changes_before = []
    selected = []

    def revoked(*args, **kwargs):
        assert selected
        assert not conn.in_transaction or boundary == "enqueue_outbox"
        # enqueue_outbox normally runs inside a short transaction. Revoke at
        # its preceding BEGIN boundary in the separate test below instead.
        if conn.in_transaction:
            raise AssertionError("test interception must precede the write lock")
        _expire(other, event_pk)
        changes_before.append(conn.total_changes)
        return original(*args, **kwargs)

    def selector(*_):
        selected.append(True)
        # Research path exercises ack/job creation, not direct-answer evidence.
        return {"knowledge_id": None, "confidence": 0.99}

    if boundary == "enqueue_outbox":
        # The public wrapper enters its transaction before enqueue_outbox. Hook
        # that caller so revocation happens before SQLite serializes the enqueue.
        original = orchestrator._acknowledge_investigation
        monkeypatch.setattr(orchestrator, "_acknowledge_investigation", revoked)
    else:
        monkeypatch.setattr(orchestrator, boundary, revoked)
    try:
        with pytest.raises(InboundClaimLost):
            process_inbound(
                conn,
                event_pk=event_pk,
                worker_id="boundary-instance",
                config=cfg,
                semantic_selector=selector,
            )
        assert knowledge_id
        assert len(changes_before) == 1
        assert conn.total_changes == changes_before[0]
    finally:
        other.close()


@pytest.mark.parametrize(
    "api", ["execute", "executemany", "cursor", "context", "begin"]
)
def test_guarded_writable_apis_reject_lost_claim(conn, config, api):
    event_pk = _event(conn)
    guarded, claim = _guard(conn, event_pk)
    other = connect(config.database_path)
    try:
        _expire(other, event_pk)
        successor = claim_inbound(other, worker_id=claim.worker_id)[0]
        before = conn.total_changes
        with pytest.raises(InboundClaimLost):
            if api == "execute":
                guarded.execute("UPDATE inbound_events SET last_error='stale'")
            elif api == "executemany":
                guarded.executemany(
                    "UPDATE inbound_events SET last_error=?", [("stale",)]
                )
            elif api == "cursor":
                guarded.cursor().execute("UPDATE inbound_events SET last_error='stale'")
            elif api == "context":
                with guarded:
                    guarded.execute("UPDATE inbound_events SET last_error='stale'")
            else:
                guarded.execute("BEGIN")
        assert not conn.in_transaction
        assert conn.total_changes == before
        assert not fail_claim(conn, claim, RuntimeError("late"))
        assert (
            conn.execute("SELECT claim_token FROM inbound_events").fetchone()[0]
            == successor.claim_token
        )
        assert (
            conn.execute("SELECT last_error FROM inbound_events").fetchone()[0] is None
        )
    finally:
        other.close()


def test_guard_preserves_rows_cursor_and_nested_savepoint_transactions(conn):
    guarded, _ = _guard(conn, _event(conn))
    assert guarded.row_factory is sqlite3.Row
    with transaction(guarded):
        guarded.execute("UPDATE inbound_events SET last_error='outer'")
        guarded.execute("SAVEPOINT nested")
        guarded.cursor().executemany(
            "UPDATE inbound_events SET last_error=?", [("first",), ("second",)]
        )
        guarded.execute("ROLLBACK TO nested")
        guarded.execute("RELEASE nested")
        row = guarded.cursor().execute("SELECT * FROM inbound_events").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["last_error"] == "outer"
        cursor = guarded.execute("SELECT * FROM inbound_events")
        assert iter(cursor) is cursor
        assert iter(cursor).connection is guarded
        assert next(cursor)["last_error"] == "outer"
    guarded.execute("SAVEPOINT root")
    guarded.execute("UPDATE inbound_events SET last_error='root'")
    guarded.execute("SAVEPOINT child")
    guarded.execute("UPDATE inbound_events SET last_error='child'")
    guarded.execute("ROLLBACK TO root")
    guarded.execute("RELEASE root")
    assert not conn.in_transaction
    assert (
        conn.execute("SELECT last_error FROM inbound_events").fetchone()[0] == "outer"
    )
    with pytest.raises(AttributeError):
        guarded.executescript("UPDATE inbound_events SET last_error='unguarded'")
    with pytest.raises(AttributeError):
        guarded.cursor().executescript(
            "UPDATE inbound_events SET last_error='unguarded'"
        )
    with pytest.raises(sqlite3.ProgrammingError):
        guarded.execute("PRAGMA writable_schema=ON")
    with pytest.raises(sqlite3.ProgrammingError):
        guarded.executemany("COMMIT", [()])
    cursor = guarded.executemany(
        "UPDATE inbound_events SET last_error=?", [("first",), ("second",)]
    )
    assert cursor.rowcount == 2


def test_terminal_update_is_last_and_stop_before_commit_rolls_it_back(conn):
    stop = threading.Event()
    guarded, _ = _guard(conn, _event(conn), stop_requested=stop.is_set)
    with pytest.raises(InboundClaimLost, match="last operation"), transaction(guarded):
        guarded.finish("processed")
        guarded.execute("UPDATE inbound_events SET last_error='late'")
    assert conn.execute("SELECT status FROM inbound_events").fetchone()[0] == "claimed"
    with pytest.raises(InboundClaimLost, match="stop requested"), transaction(guarded):
        guarded.finish("processed")
        stop.set()
    assert not conn.in_transaction
    assert conn.execute("SELECT status FROM inbound_events").fetchone()[0] == "claimed"


def test_current_attempt_failure_is_retried_for_direct_cli_caller(conn, config):
    _knowledge(conn)

    def failed(*_):
        raise RuntimeError("model unavailable")

    with pytest.raises(RuntimeError, match="model unavailable"):
        process_inbound(
            conn,
            event_pk=_event(conn),
            worker_id="cli-instance",
            config=_config(config),
            semantic_selector=failed,
        )
    row = conn.execute("SELECT * FROM inbound_events").fetchone()
    assert row["status"] == "new"
    assert row["lease_owner"] is None
    assert row["next_attempt_at"] is not None
    assert row["last_error"] == "RuntimeError: model unavailable"


@pytest.mark.parametrize(
    "action", ["takeover", "pause_resume", "claim", "claim_delegate", "new_input"]
)
@pytest.mark.parametrize("late_error", [False, True])
def test_slow_diagnostic_cannot_publish_after_case_or_turn_takeover(
    conn, config, action, late_error
):
    from test_routing import route_value

    from k3_support.coordination import control_communication
    from k3_support.store import transition_case

    cfg = _config(config)
    event_pk = _event(conn)
    other = connect(cfg.database_path)
    changes_at_takeover = []

    def diagnostic(context):
        case_id = context["case_id"]
        assert not conn.in_transaction
        case = other.execute(
            "SELECT state,version FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if action in {"takeover", "pause_resume"}:
            version = transition_case(
                other,
                case_id=case_id,
                after="takeover" if action == "takeover" else "paused",
                actor_type="operator",
                actor_id="owner",
                reason="test human intervention",
                expected_version=case["version"],
            )
            if action == "pause_resume":
                transition_case(
                    other,
                    case_id=case_id,
                    after=case["state"],
                    actor_type="operator",
                    actor_id="owner",
                    reason="fresh delegation",
                    expected_version=version,
                )
        elif action == "new_input":
            from k3_support.conversation_context import admit_im_event

            admit_im_event(
                other,
                cfg,
                {
                    "source": "feishu_user_poll",
                    "identity": "user",
                    "external_id": "om_new_during_diagnostic",
                    "sender_id": "ou_peer",
                    "chat_id": "oc_peer",
                    "thread_id": "om_lease",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "payload": {
                        "content": "更正：现在是 EVB",
                        "chat_type": "p2p",
                        "root_id": "om_lease",
                    },
                },
            )
        else:
            control_communication(
                other,
                case_id=case_id,
                action="claim",
                actor_id="owner",
                external_id="inbound-human-claim",
            )
            if action == "claim_delegate":
                control_communication(
                    other,
                    case_id=case_id,
                    action="delegate",
                    actor_id="owner",
                    external_id="inbound-human-delegate",
                )
        changes_at_takeover.append(conn.total_changes)
        if late_error:
            raise ValueError("late diagnostic failure")
        return {
            "facts": {"actual": "test observation"},
            "missing": [],
            "confidence": 0.9,
        }

    try:

        def process():
            return process_inbound(
                conn,
                event_pk=event_pk,
                worker_id="diagnostic-instance",
                config=cfg,
                message_router=lambda _: route_value("codex_debug", issue_type="bug"),
                diagnostic_extractor=diagnostic,
            )

        if action in {"claim", "claim_delegate", "new_input"}:
            result = process()
            assert result["superseded"] and not result["processed"]
            assert conn.execute(
                "SELECT status,last_error FROM inbound_events WHERE event_pk=?",
                (event_pk,),
            ).fetchone()[:] == ("ignored", "context_superseded")
            # Only the exact-token terminal write is allowed after withdrawal.
            assert (
                changes_at_takeover and conn.total_changes == changes_at_takeover[0] + 1
            )
        else:
            with pytest.raises(InboundClaimLost):
                process()
            assert changes_at_takeover and conn.total_changes == changes_at_takeover[0]
        assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 1
        for table in ("diagnostic_snapshots", "case_suggestions", "outbox", "jobs"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT count(*) FROM case_events WHERE event_type='decision_applied'"
            ).fetchone()[0]
            == 0
        )
    finally:
        other.close()


def test_slow_contact_return_cannot_update_profile_or_make_another_lookup(conn, config):
    from k3_support.lark import CommandResult

    raw = copy.deepcopy(_config(config).raw)
    raw["routing"]["org_profile_lookup"] = True
    cfg = Config(validate_config(raw), config.path)
    event_pk = _event(conn)
    other = connect(cfg.database_path)
    calls = []

    def contact(args):
        assert not conn.in_transaction
        calls.append(args)
        _expire(other, event_pk)
        return CommandResult(
            {"user": {"open_id": "ou_peer", "name": "fixture"}}, "user", []
        )

    try:
        with pytest.raises(InboundClaimLost):
            process_inbound(
                conn,
                event_pk=event_pk,
                worker_id="contact-instance",
                config=cfg,
                contact_runner=contact,
            )
        assert len(calls) == 1
        assert (
            conn.execute("SELECT count(*) FROM requester_profiles").fetchone()[0] == 0
        )
        assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 0
    finally:
        other.close()


def test_heartbeat_connection_error_is_fail_closed(conn, config, monkeypatch):
    from k3_support import inbound_claims

    knowledge_id = _knowledge(conn)
    failed = threading.Event()

    def broken_connection(*_):
        failed.set()
        raise sqlite3.OperationalError("test renewal connection unavailable")

    def selector(*_):
        assert failed.wait(5)
        time.sleep(0.02)
        return _selection(knowledge_id)

    monkeypatch.setattr(inbound_claims, "connect", broken_connection)
    with pytest.raises(InboundClaimLost):
        process_inbound(
            conn,
            event_pk=_event(conn),
            worker_id="broken-heartbeat",
            config=_config(config),
            semantic_selector=selector,
        )
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_v24_upgrade_requeues_only_legacy_claims_and_preserves_terminal_history(
    tmp_path, monkeypatch
):
    from k3_support import db

    connection = connect(tmp_path / "legacy-inbound.db")
    files = db.migration_files()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                db, "migration_files", lambda: [item for item in files if item[0] <= 24]
            )
            db.migrate(connection)
        for status in ("new", "claimed", "processed", "ignored", "dead_letter"):
            event_pk = _event(connection, f"legacy-{status}")
            connection.execute(
                "UPDATE inbound_events SET status=? WHERE event_pk=?",
                (status, event_pk),
            )
        connection.execute(
            "UPDATE inbound_events SET lease_owner='old-pid',lease_expires_at=?,attempt_count=3 WHERE status='claimed'",
            ((datetime.now(UTC) + timedelta(hours=1)).isoformat(),),
        )
        assert db.migrate(connection) == [
            version for version, _, _ in files if version > 24
        ]
        rows = {
            row["external_id"]: dict(row)
            for row in connection.execute("SELECT * FROM inbound_events")
        }
        assert rows["legacy-claimed"]["status"] == "new"
        assert rows["legacy-claimed"]["lease_owner"] is None
        assert rows["legacy-claimed"]["claim_token"] is None
        assert rows["legacy-claimed"]["attempt_count"] == 3
        for status in ("processed", "ignored", "dead_letter"):
            assert rows[f"legacy-{status}"]["status"] == status
        handles = claim_inbound(connection, worker_id="new-process", limit=10)
        assert len(handles) == 2
        assert len({handle.claim_token for handle in handles}) == 2
        assert db.integrity(connection)["ok"]
    finally:
        connection.close()


def test_worker_instances_use_unique_identity_and_never_apply_raw_late_failure(
    conn, config, monkeypatch
):
    from k3_support import services

    worker_ids = []
    successors = []

    def stale_processing(connection, **kwargs):
        worker_ids.append(kwargs["worker_id"])
        handle = kwargs["event_pk"]
        assert handle.claim_token
        _expire(connection, handle)
        successor = claim_inbound(connection, worker_id="replacement-worker")[0]
        assert successor.claim_token != handle.claim_token
        successors.append(
            dict(
                connection.execute(
                    "SELECT * FROM inbound_events WHERE event_pk=?", (str(handle),)
                ).fetchone()
            )
        )
        raise RuntimeError("late old-process failure")

    monkeypatch.setattr(services, "process_inbound", stale_processing)
    monkeypatch.setattr(
        services, "_run", lambda component, tick, interval: tick(conn, config)
    )
    monkeypatch.setattr(services.Stop, "requested", False)
    for number in range(2):
        event_pk = _event(conn, f"worker-instance-{number}")
        services.worker_main()
        assert (
            dict(
                conn.execute(
                    "SELECT * FROM inbound_events WHERE event_pk=?", (event_pk,)
                ).fetchone()
            )
            == successors[-1]
        )
    assert len(worker_ids) == len(set(worker_ids)) == 2


@pytest.mark.parametrize(
    "completed_boundary", ["create_case", "apply_decision", "create_retrieval_job"]
)
def test_reclaim_after_partial_commits_finishes_without_duplicate_case_reply_or_job(
    conn, config, monkeypatch, completed_boundary
):
    from k3_support import orchestrator

    cfg = _config(config)
    knowledge_id = _knowledge(conn)
    event_pk = _event(conn)
    other = connect(cfg.database_path)
    original = getattr(orchestrator, completed_boundary)
    completed = []

    def expire_after_commit(*args, **kwargs):
        result = original(*args, **kwargs)
        assert not conn.in_transaction
        if not completed:
            completed.append(True)
            _expire(other, event_pk)
        return result

    def selector(*_):
        return (
            _selection(knowledge_id)
            if completed_boundary != "create_retrieval_job"
            else {
                "knowledge_id": None,
                "confidence": 0.99,
            }
        )

    monkeypatch.setattr(orchestrator, completed_boundary, expire_after_commit)
    try:
        with pytest.raises(InboundClaimLost):
            process_inbound(
                conn,
                event_pk=event_pk,
                worker_id="same-worker",
                config=cfg,
                semantic_selector=selector,
            )
        assert completed
        handle = claim_inbound(other, worker_id="same-worker")[0]
        result = process_inbound(
            other,
            event_pk=handle,
            worker_id="same-worker",
            config=cfg,
            semantic_selector=selector,
        )
        assert result["processed"]
        assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM route_decisions").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT count(*) FROM outbox WHERE channel='feishu_im'"
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == (
            completed_boundary == "create_retrieval_job"
        )
        assert (
            conn.execute("SELECT status FROM inbound_events").fetchone()[0]
            == "processed"
        )
    finally:
        other.close()


def test_15_minute_inbound_lease_updates_real_service_health_and_detects_death(
    conn, config
):
    from k3_support.watchdog import collect_health_alerts

    _, claim = _guard(conn, _event(conn))
    start = datetime.now(UTC)
    publish_worker_health(
        conn,
        claim.worker_id,
        "ready",
        {"heartbeat_phase": "idle"},
        register=True,
        now=start,
    )
    other = connect(config.database_path)
    try:
        renew_inbound_claim(
            other, claim, report_worker_health=True, bind_worker=True, now=start
        )
        for interval in range(1, 31):
            observed = start + timedelta(seconds=30 * interval)
            renew_inbound_claim(other, claim, report_worker_health=True, now=observed)
            alerts = collect_health_alerts(conn, config, now=observed)
            assert not [
                alert
                for alert in alerts
                if alert["detail"].get("component") == "worker"
            ]
        health = conn.execute(
            "SELECT * FROM service_state WHERE component='worker'"
        ).fetchone()
        assert health["heartbeat_at"] == observed.isoformat()
        assert json.loads(health["detail_json"])["claim_token"] == claim.token
        alerts = collect_health_alerts(
            conn, config, now=observed + timedelta(minutes=4)
        )
        assert [
            alert["key"]
            for alert in alerts
            if alert["detail"].get("component") == "worker"
        ] == ["heartbeat_stale:worker"]
        publish_worker_health(
            conn,
            "replacement-instance",
            "ready",
            {"heartbeat_phase": "idle"},
            register=True,
            now=observed,
        )
        snapshot = dict(
            conn.execute(
                "SELECT * FROM service_state WHERE component='worker'"
            ).fetchone()
        )
        with pytest.raises(InboundClaimLost, match="superseded worker"):
            renew_inbound_claim(
                other,
                claim,
                report_worker_health=True,
                now=observed + timedelta(seconds=1),
            )
        assert not publish_worker_health(
            other, claim.worker_id, "ready", {"heartbeat_phase": "running"}
        )
        assert (
            dict(
                conn.execute(
                    "SELECT * FROM service_state WHERE component='worker'"
                ).fetchone()
            )
            == snapshot
        )
    finally:
        other.close()


def test_inbound_heartbeat_thread_database_failure_is_observable_and_fenced(
    conn, config, monkeypatch
):
    from k3_support import inbound_claims
    from k3_support.watchdog import collect_health_alerts

    _, claim = _guard(conn, _event(conn))
    publish_worker_health(
        conn, claim.worker_id, "ready", {"heartbeat_phase": "idle"}, register=True
    )
    original = inbound_claims.renew_inbound_claim

    def fail_renewal(*args, **kwargs):
        if kwargs.get("bind_worker"):
            return original(*args, **kwargs)
        raise sqlite3.OperationalError("fixture renewal failed")

    monkeypatch.setattr(inbound_claims, "renew_inbound_claim", fail_renewal)
    monitor = InboundHeartbeat(
        config.database_path,
        claim,
        lease_seconds=120,
        interval_seconds=0.01,
        stop_requested=lambda: False,
        report_worker_health=True,
    )
    try:
        assert monitor.failed.wait(5)
        monitor.close()  # Wait for its independent failure-status write.
        with pytest.raises(InboundClaimLost) as caught:
            monitor.check()
        assert caught.value.error_class == "OperationalError"
        health = conn.execute(
            "SELECT * FROM service_state WHERE component='worker'"
        ).fetchone()
        assert health["status"] == "degraded"
        assert json.loads(health["detail_json"])["error_class"] == "OperationalError"
        assert [
            alert["key"]
            for alert in collect_health_alerts(conn, config)
            if alert["detail"].get("component") == "worker"
        ] == ["component_unhealthy:worker"]
    finally:
        monitor.close()


@pytest.mark.parametrize("health_error", [None, "OperationalError"])
def test_worker_distinguishes_normal_claim_loss_from_sticky_renewal_failure(
    conn, config, monkeypatch, health_error
):
    from k3_support import services

    _event(conn)
    outputs = []
    calls = []

    def lost(*args, **kwargs):
        if calls:
            return {"processed": False, "reason": "duplicate"}
        calls.append(True)
        raise InboundClaimLost("test lost claim", error_class=health_error)

    def run(component, tick, interval):
        outputs.append(tick(conn, config))
        _event(conn, "health-duplicate")
        outputs.append(tick(conn, config))  # A duplicate is not successful processing.
        outputs.append(tick(conn, config))  # Neither may an idle tick hide the failure.

    monkeypatch.setattr(services, "process_inbound", lost)
    monkeypatch.setattr(services, "_run", run)
    monkeypatch.setattr(services.Stop, "requested", False)
    services.worker_main()
    assert [output["ready"] for output in outputs] == [health_error is None] * 3
    status = conn.execute(
        "SELECT status FROM service_state WHERE component='worker'"
    ).fetchone()[0]
    assert status == ("ready" if health_error is None else "degraded")
