"""Real review fixtures, no execution during final evidence reconstruction."""

import json
import sqlite3

import pytest
from test_release_crossreview import _case_reply

from k3_support.ids import canonical_json
from k3_support.review import ReviewError, _verified_board_cleanup
from k3_support.review_evidence import verified_non_git_evidence
from k3_support.review_evidence import verified_board_output
from k3_support.store import create_case
from k3_support.timeutil import iso_now


def _records(conn, config, kind):
    _case_reply(conn, config, kind)
    review = conn.execute("SELECT * FROM codex_reviews WHERE status='decision_applied'").fetchone()
    item = conn.execute(
        """SELECT e.* FROM evidence e JOIN case_sources s USING(source_id)
             WHERE s.source_type=? ORDER BY e.created_at,e.evidence_id""",
        ("board1_session" if kind == "board" else "gerrit_wip",),
    ).fetchone()
    return review, item


def _verify(conn, review, item):
    return verified_non_git_evidence(
        conn, review=review, item=item, check=json.loads(item["result"]),
        checks=json.loads(review["independent_checks_json"]),
    )


def test_board_output_projection_preserves_receipt_and_rejects_tampering(conn, config):
    review, item = _records(conn, config, 'board')
    value = verified_board_output(conn, review=review, item=item)
    assert value is not None
    assert value['evidence_id'] == item['evidence_id']
    assert value['observed_at'] and value['session_id']
    assert value['output_digest'] == json.loads(item['result'])['output_digest']
    conn.execute("UPDATE action_ledger SET result_json=json_set(result_json,'$.stdout','changed') WHERE action_type='board'")
    assert verified_board_output(conn, review=review, item=item) is None


def test_push_output_cannot_become_board_measurement(conn, config):
    review, item = _records(conn, config, 'push')
    assert verified_board_output(conn, review=review, item=item) is None


@pytest.mark.parametrize("kind", ["board", "push"])
def test_non_git_reconstruction_uses_only_selects(conn, config, kind):
    review, item = _records(conn, config, kind)
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
    attempted = []

    def readonly(action, *_args):
        if action not in allowed:
            attempted.append(action)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(readonly)
    try:
        assert _verify(conn, review, item)
        assert not attempted
    finally:
        conn.set_authorizer(None)


@pytest.mark.parametrize("same_session", [False, True])
def test_board_receipt_survives_a_new_session_but_not_its_own_unclosed_lock(conn, config, same_session):
    review, item = _records(conn, config, "board")
    session = json.loads(conn.execute("SELECT metadata_json FROM case_sources WHERE source_id=?",
                                     (item["source_id"],)).fetchone()[0])["session_id"]
    case_id = review["case_id"]
    if not same_session:
        case_id, _ = create_case(conn, title="another board task", case_type="bug", severity="P2", confidence=0.8)
    now = iso_now()
    conn.execute(
        """INSERT INTO locks(lock_key,owner,case_id,scope,acquired_at,expires_at,heartbeat_at,metadata_json)
             VALUES('board1','fixture',?,'board1',?,?,?,?)""",
        (case_id, now, now, now, canonical_json({"session_id": session if same_session else "another-session"})),
    )
    # Review preparation still requires the board to be free. Replaying a
    # completed historical receipt does not claim the currently busy board.
    with pytest.raises(ReviewError):
        _verified_board_cleanup(conn, case_id=review["case_id"], session_id=session)
    assert _verify(conn, review, item) is not same_session


@pytest.mark.parametrize("change", [
    "session", "job_session", "ledger_input", "ledger_stdout", "cleanup_review",
    "cleanup_attempt", "unknown_source", "malformed_result",
])
def test_board_exact_receipts_cannot_be_relabelled(conn, config, change):
    review, item = _records(conn, config, "board")
    assert _verify(conn, review, item)
    if change in {"session", "unknown_source"}:
        if change == "session":
            metadata = {"board": "board1", "session_id": "a-different-session"}
            conn.execute("UPDATE case_sources SET metadata_json=? WHERE source_id=?",
                         (canonical_json(metadata), item["source_id"]))
        else:
            conn.execute("UPDATE case_sources SET source_type='untrusted_note' WHERE source_id=?",
                         (item["source_id"],))
    elif change == "job_session":
        conn.execute("UPDATE jobs SET context_json=json_set(context_json,'$.board_session_id','other') WHERE job_id=?",
                     (review["job_id"],))
    elif change == "cleanup_review":
        checks = json.loads(review["independent_checks_json"])
        checks = [check for check in checks if check.get("kind") != "board_cleanup"]
        conn.execute("UPDATE codex_reviews SET independent_checks_json=? WHERE review_id=?",
                     (canonical_json(checks), review["review_id"]))
    elif change == "cleanup_attempt":
        key = conn.execute("SELECT action_key FROM action_ledger WHERE action_key LIKE '%:cleanup:%' LIMIT 1").fetchone()[0]
        conn.execute("UPDATE action_ledger SET action_key=? WHERE action_key=?", (key + "changed", key))
    elif change == "ledger_input":
        conn.execute("UPDATE action_ledger SET input_digest='different' WHERE action_type='board'")
    elif change == "ledger_stdout":
        conn.execute("UPDATE action_ledger SET result_json=json_set(result_json,'$.stdout','different') WHERE action_type='board'")
    else:
        conn.execute("UPDATE action_ledger SET result_json='[]' WHERE action_type='board'")
    review = conn.execute("SELECT * FROM codex_reviews WHERE review_id=?", (review["review_id"],)).fetchone()
    assert not _verify(conn, review, item)


@pytest.mark.parametrize("change", [
    "source_version", "source_project", "approval_action", "job_review", "event_receipt",
    "ledger_revision", "ledger_project", "ledger_patch_set", "ledger_wip", "ledger_input",
])
def test_gerrit_exact_approval_and_readback_are_required(conn, config, change):
    review, item = _records(conn, config, "push")
    assert _verify(conn, review, item)
    if change in {"source_version", "source_project"}:
        column = "source_version" if change == "source_version" else "stable_external_id"
        conn.execute(f"UPDATE case_sources SET {column}='different' WHERE source_id=?", (item["source_id"],))
    elif change == "approval_action":
        conn.execute("UPDATE approvals SET requested_action_json=json_set(requested_action_json,'$.repo','other') WHERE approval_type='wip_push'")
    elif change == "job_review":
        conn.execute("UPDATE jobs SET context_json=json_set(context_json,'$.review_id','other') WHERE job_type='push'")
    elif change == "event_receipt":
        conn.execute("UPDATE case_events SET detail_json=json_set(detail_json,'$.verification.patch_set',999) WHERE event_type='wip_push_verified'")
    elif change == "ledger_input":
        conn.execute("UPDATE action_ledger SET input_digest='different' WHERE action_type='push'")
    else:
        field = change.removeprefix("ledger_")
        conn.execute("UPDATE action_ledger SET result_json=json_set(result_json,?,?) WHERE action_type='push'",
                     (f"$.verification.{field}", 0 if field == "wip" else "different"))
    assert not _verify(conn, review, item)
