"""Read-only consistency checks for independently reviewed execution evidence.

The caller must first bind the complete review, decision and Outbox. These
records are not an OS privilege boundary against a writer with the same UID.
No missing evidence is repaired, executed again, or promoted here.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from .ids import digest


def _output_hash(result: dict) -> str | None:
    if (type(result.get("returncode")) is not int or result["returncode"] != 0
            or not isinstance(result.get("stdout"), str)
            or not isinstance(result.get("stderr"), str)):
        return None
    return hashlib.sha256((result["stdout"] + "\0" + result["stderr"]).encode()).hexdigest()


def _board_ledger(conn, *, case_id: str, action: dict, action_key: str):
    row = conn.execute(
        "SELECT * FROM action_ledger WHERE action_key=? AND case_id=?",
        (action_key, case_id),
    ).fetchone()
    if (row is None or row["action_type"] != "board" or row["state"] != "verified"
            or row["input_digest"] != digest(action) or not row["finished_at"]):
        return None
    output_hash = _output_hash(json.loads(row["result_json"]))
    return (row, output_hash) if output_hash else None


def _board(conn, *, review, item, source, metadata, job, check, checks) -> bool:
    from .review import _verified_board_cleanup

    session = metadata.get("session_id")
    if (not isinstance(session, str) or not session or metadata.get("board") != "board1"
            or source["stable_external_id"] != session or source["source_version"] != session
            or json.loads(job["context_json"]).get("board_session_id") != session
            or set(check) != {"action", "output_digest", "returncode", "verified"}
            or type(check["returncode"]) is not int or check["returncode"] != 0):
        return False
    case_id = review["case_id"]
    expected_source = f"src_{digest({'case': case_id, 'session': session, 'board': 'board1'})[:32]}"
    if source["source_id"] != expected_source:
        return False
    lease = conn.execute(
        """SELECT 1 FROM approvals WHERE case_id=? AND session_id=?
             AND approval_type='board1_lease' AND status='consumed'
             AND consumed_at IS NOT NULL AND lifecycle_round=?""",
        (case_id, session, job["lifecycle_round"]),
    ).fetchone()
    if lease is None:
        return False
    # Recompute both cleanup receipts and require the exact same attempt that
    # the independent review actually saw, including fresh output digests.
    cleanup = _verified_board_cleanup(
        conn, case_id=case_id, session_id=session, require_unoccupied=False,
    )
    for receipt in cleanup:
        if receipt not in checks:
            return False
        key = f"{case_id}:board1:{session}:{digest(receipt['action'])}"
        if receipt["cleanup_attempt_id"] != "legacy":
            key += f":cleanup:{receipt['cleanup_attempt_id']}"
        actual = _board_ledger(conn, case_id=case_id, action=receipt["action"], action_key=key)
        if (actual is None or actual[1] != receipt["output_digest"]
                or actual[0]["finished_at"] != receipt["finished_at"]):
            return False
    action = check["action"]
    if (not isinstance(action, dict)
            or action.get("type") not in {"ram_boot", "serial_wait", "serial_exec", "enter_brom"}
            or item["evidence_layer"] != ("ram_boot" if action["type"] == "ram_boot" else "device_function")
            or item["artifact_hash"] != check["output_digest"]):
        return False
    base = f"{case_id}:board1:{session}:{digest(action)}"
    prefix = base + ":cleanup:"
    for row in conn.execute(
        """SELECT action_key FROM action_ledger WHERE case_id=?
             AND (action_key=? OR substr(action_key,1,length(?))=?)""",
        (case_id, base, prefix, prefix),
    ):
        key = row["action_key"]
        if key != base and (action["type"] not in {"enter_brom", "serial_wait"}
                            or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", key[len(prefix):])):
            continue
        actual = _board_ledger(conn, case_id=case_id, action=action, action_key=key)
        if actual is None or actual[1] != check["output_digest"]:
            continue
        expected_id = f"evd_{digest({'action_key': key, 'output': actual[1]})[:32]}"
        if item["evidence_id"] == expected_id:
            return True
    return False


def _push(conn, *, review, item, source, metadata, job, check, checks) -> bool:
    from .executors import validate_wip_action

    if check not in checks or check.get("kind") != "gerrit_wip" or metadata.get("reviewed_independently") is not True:
        return False
    push = conn.execute(
        "SELECT * FROM jobs WHERE job_id=? AND case_id=? AND job_type='push'",
        (metadata.get("push_job_id"), review["case_id"]),
    ).fetchone()
    if (push is None or push["state"] != "succeeded" or push["exit_code"] != 0
            or push["lifecycle_round"] != job["lifecycle_round"]
            or push["max_attempts"] != 1 or push["attempt_no"] != 1):
        return False
    context = json.loads(push["context_json"])
    if (set(context) != {"action", "action_digest", "approval_id", "origin_job_id", "review_id"}
            or context["origin_job_id"] != review["job_id"] or context["review_id"] != review["review_id"]
            or context["approval_id"] != metadata.get("approval_id")):
        return False
    action = context["action"]
    validate_wip_action(action)
    action_digest = digest(action)
    if (action["case_id"] != review["case_id"] or action_digest != context["action_digest"]
            or push["input_digest"] != digest({"approval_id": context["approval_id"],
                                              "action_digest": action_digest,
                                              "review_id": review["review_id"]})):
        return False
    approval = conn.execute(
        """SELECT * FROM approvals WHERE approval_id=? AND case_id=?
             AND approval_type='wip_push' AND status='consumed' AND consumed_at IS NOT NULL""",
        (context["approval_id"], review["case_id"]),
    ).fetchone()
    if (approval is None or approval["lifecycle_round"] != job["lifecycle_round"]
            or approval["action_digest"] != action_digest
            or json.loads(approval["requested_action_json"]) != action):
        return False
    ledger = conn.execute(
        "SELECT * FROM action_ledger WHERE action_key=? AND case_id=?",
        (f"{review['case_id']}:push:{action_digest}", review["case_id"]),
    ).fetchone()
    if (ledger is None or ledger["action_type"] != "push" or ledger["state"] != "verified"
            or ledger["input_digest"] != action_digest or not ledger["finished_at"]):
        return False
    result = json.loads(ledger["result_json"])
    if _output_hash(result["execution"]) is None:
        return False
    verification = result["verification"]
    binding = action["binding"]
    if (verification.get("wip") is not True or verification.get("errors")
            or verification.get("revision") != binding["tip_sha"]
            or verification.get("project") != binding["project"]
            or verification.get("branch") != binding["destination_branch"]
            or not verification.get("change") or not verification.get("patch_set")
            or str(ledger["remote_id"]) != str(verification["change"])
            or push["output_digest"] != digest(verification)):
        return False
    changes = verification.get("changes")
    if (not isinstance(changes, list) or len(changes) != len(action["commits"])
            or any(not isinstance(change, dict) or change.get("revision") != commit
                   or change.get("project") != binding["project"]
                   or change.get("branch") != binding["destination_branch"]
                   or change.get("wip") is not True or not change.get("change")
                   or not change.get("patch_set")
                   for change, commit in zip(changes, action["commits"], strict=True))):
        return False
    if any(changes[-1].get(key) != verification.get(key)
           for key in ("change", "revision", "patch_set", "wip", "project", "branch")):
        return False
    expected_check = {
        "kind": "gerrit_wip", "repo": action["repo"], "destination": action["destination"],
        "approved_action_digest": action_digest, "change": verification["change"],
        "revision": verification["revision"], "patch_set": verification["patch_set"],
        "wip": True, "changes": changes, "project": verification["project"],
        "branch": verification["branch"], "verified": True,
    }
    source_id = f"src_{digest({'job_id': push['job_id'], 'gerrit': expected_check})[:32]}"
    evidence_id = f"evd_{digest({'source_id': source_id, 'check': expected_check})[:32]}"
    if (check != expected_check or source["source_id"] != source_id or item["evidence_id"] != evidence_id
            or source["stable_external_id"] != f"{verification['project']}:{verification['change']}"
            or source["source_version"] != f"{verification['revision']}:{verification['patch_set']}"
            or item["artifact_hash"] != verification["revision"] or item["evidence_layer"] != "static"):
        return False
    event = conn.execute(
        """SELECT * FROM case_events WHERE case_id=? AND event_type='wip_push_verified'
             AND actor_type='system' AND actor_id=? AND idempotency_key=?""",
        (review["case_id"], push["job_id"], f"job:{push['job_id']}:wip-verified"),
    ).fetchone()
    return event is not None and json.loads(event["detail_json"]) == {
        "approval_id": context["approval_id"], "evidence_id": evidence_id, "verification": verification,
    }


def _require_review_content(conn, review):
    from .content_retirement import require_case_content
    job = conn.execute('SELECT lifecycle_round FROM jobs WHERE job_id=? AND case_id=?',
                       (review['job_id'], review['case_id'])).fetchone()
    if job is None:
        raise ValueError('review job missing')
    require_case_content(conn, case_id=review['case_id'], lifecycle_round=job['lifecycle_round'])


def verified_board_output(conn, *, review, item):
    """Return exact reviewed board output, or None; never accept a caller's flag.

    This is a DB-consistency projection, not proof of physical sensor truth or
    protection against a same-UID database writer. Consumers must parse output
    conservatively and preserve observation time and component identity.
    """
    try:
        _require_review_content(conn, review)
        check = json.loads(item['result'])
        checks = json.loads(review['independent_checks_json'])
        source = conn.execute('SELECT * FROM case_sources WHERE source_id=?',
                              (item['source_id'],)).fetchone()
        if source is None or source['source_type'] != 'board1_session':
            return None
        if not verified_non_git_evidence(conn, review=review, item=item, check=check, checks=checks):
            return None
        session = json.loads(source['metadata_json'])['session_id']
        base = f"{review['case_id']}:board1:{session}:{digest(check['action'])}"
        prefix = base+':cleanup:'
        for row in conn.execute(
            'SELECT * FROM action_ledger WHERE case_id=? AND (action_key=? OR substr(action_key,1,length(?))=?)',
            (review['case_id'], base, prefix, prefix)):
            result = json.loads(row['result_json'])
            output = _output_hash(result)
            expected_id = f"evd_{digest({'action_key': row['action_key'], 'output': output})[:32]}"
            if output == check['output_digest'] and expected_id == item['evidence_id']:
                return {'board': 'board1', 'session_id': session,
                        'evidence_id': item['evidence_id'], 'observed_at': row['finished_at'],
                        'action': check['action'], 'stdout': result['stdout'],
                        'output_digest': output, 'verification': 'reviewed_execution_receipt'}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, sqlite3.Error):
        return None
    return None


def verified_non_git_evidence(
    conn: sqlite3.Connection, *, review: sqlite3.Row, item: sqlite3.Row,
    check: dict, checks: list,
) -> bool:
    """Accept only exact, current-round board or approved WIP evidence; never write."""
    from .executors import ExecutorError
    from .review import ReviewError

    try:
        _require_review_content(conn, review)
        if (not isinstance(check, dict) or check.get("verified") is not True
                or not isinstance(checks, list) or checks != json.loads(review["independent_checks_json"])
                or check != json.loads(item["result"]) or item["case_id"] != review["case_id"]
                or item["evidence_id"] not in json.loads(review["evidence_ids_json"])):
            return False
        source = conn.execute(
            "SELECT * FROM case_sources WHERE source_id=? AND case_id=?",
            (item["source_id"], review["case_id"]),
        ).fetchone()
        job = conn.execute(
            """SELECT j.*,c.lifecycle_round AS current_round FROM jobs j
                 JOIN cases c ON c.case_id=j.case_id WHERE j.job_id=? AND j.case_id=?""",
            (review["job_id"], review["case_id"]),
        ).fetchone()
        if (source is None or source["requester_access"] != "allowed" or job is None
                or job["job_type"] != "codex" or job["state"] != "succeeded"
                or job["lifecycle_round"] != job["current_round"]):
            return False
        metadata = json.loads(source["metadata_json"])
        verifier = {"board1_session": _board, "gerrit_wip": _push}.get(source["source_type"])
        if verifier is None or not isinstance(metadata, dict):
            return False
        return verifier(conn, review=review, item=item, source=source, metadata=metadata,
                        job=job, check=check, checks=checks)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, sqlite3.Error,
            ReviewError, ExecutorError):
        return False
