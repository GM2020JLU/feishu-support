"""Control-owned Bug identity and investigation history, with no remote effects.

These functions are internal services, not authentication boundaries. HTTP/chat
entry points must authenticate before calling; coding workers never access this DB.
Remote snapshots must come from the trusted Project adapter, not model reports.
"""

import json
import re

from .db import atomic, transaction
from .ids import canonical_json, digest, new_id
from .timeutil import iso_now, parse_iso, utc_now


class BugConflict(ValueError):
    pass


def _text(value, label, limit=256):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid {label}")
    return value


def _bug(conn, bug_id):
    row = conn.execute(
        "SELECT * FROM project_bugs WHERE bug_id=?", (bug_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Bug binding does not exist")
    return row


def _revision(row, expected):
    if type(expected) is not int or row["revision"] != expected:
        raise BugConflict("Bug changed; refresh before acting")


def _event(conn, bug_id, actor, kind, detail, round_id=None):
    conn.execute(
        "INSERT INTO project_bug_events VALUES(?,?,?,?,?,?,?)",
        (
            new_id("pbe"),
            bug_id,
            round_id,
            actor,
            kind,
            canonical_json(detail),
            iso_now(),
        ),
    )


def bind(conn, *, case_id, host, project_key, type_key, item_id, actor):
    """Bind resolved official identifiers; do not interpret URL slugs here."""
    for label, value in locals().copy().items():
        if label != "conn":
            _text(value, label)
    if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", host):
        raise ValueError(
            "host must be a canonical hostname without credentials or path"
        )
    identity = (host, project_key, type_key, item_id)
    with atomic(conn):
        case = conn.execute(
            "SELECT type FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if case is None or case["type"] != "bug":
            raise ValueError("binding requires an existing Bug Case")
        existing = conn.execute(
            "SELECT * FROM project_bugs WHERE host=? AND project_key=? AND type_key=? AND item_id=?",
            identity,
        ).fetchone()
        if existing:
            if existing["case_id"] != case_id:
                raise BugConflict("external Bug is already bound to another Case")
            return dict(existing)
        if conn.execute(
            "SELECT 1 FROM project_bugs WHERE case_id=?", (case_id,)
        ).fetchone():
            raise BugConflict("Case is already bound to another external Bug")
        bug_id = new_id("bug")
        conn.execute(
            """INSERT INTO project_bugs
            (bug_id,case_id,host,project_key,type_key,item_id,created_at) VALUES(?,?,?,?,?,?,?)""",
            (bug_id, case_id, *identity, iso_now()),
        )
        _event(conn, bug_id, actor, "bound", {"case_id": case_id})
        return dict(_bug(conn, bug_id))


def observe(
    conn,
    *,
    bug_id,
    observation_id,
    expected_sequence,
    payload,
    observed_at,
    read_source=None,
    before_record=None,
):
    """Record a read started at expected_sequence; late competing reads conflict.

    The remote adapter must supply a complete selected-field snapshot and explicit
    closure metadata. A failed/forbidden read must not call this function.
    """
    _text(observation_id, "observation ID")
    if type(expected_sequence) is not int or expected_sequence < 0:
        raise ValueError("invalid observation sequence")
    if not isinstance(payload, dict) or set(payload) != {
        "fields",
        "status_id",
        "closure",
        "remote_version",
        "schema_digest",
    }:
        raise ValueError("snapshot requires exact adapter fields")
    if not isinstance(payload["fields"], dict):
        raise ValueError("snapshot fields must be an object")  # noqa: TRY004 -- schema validation contract
    _text(payload["status_id"], "remote status ID")
    for key in ("remote_version", "schema_digest"):
        if payload[key] is not None:
            _text(payload[key], key)
    closure = payload["closure"]
    if not isinstance(closure, dict) or set(closure) != {"closed", "reason"}:
        raise ValueError("closure must be explicit")
    if closure["closed"] is not None and type(closure["closed"]) is not bool:
        raise ValueError("closure must be true, false or unknown")
    if closure["reason"] is not None:
        _text(closure["reason"], "closure reason", 2000)
    when = parse_iso(observed_at)
    if when > utc_now():
        raise ValueError("observation cannot be in the future")
    # Reject non-JSON objects and NaN before canonical persistence/digest.
    json.dumps(payload, allow_nan=False)
    signature_input = {"payload": payload, "observed_at": observed_at}
    if read_source is not None:
        if not isinstance(read_source, dict) or set(read_source) != {
            "actor",
            "grant_id",
            "evidence",
        }:
            raise ValueError("invalid read source")
        _text(read_source["actor"], "actor")
        _text(read_source["grant_id"], "read grant")
        if not isinstance(read_source["evidence"], dict):
            raise ValueError("invalid read evidence")
        json.dumps(read_source, allow_nan=False)
        signature_input["read_source"] = read_source
    signature = digest(signature_input)
    with atomic(conn):
        if before_record is not None:
            before_record()
        bug = _bug(conn, bug_id)
        if read_source is not None:
            from .project_bug_grants import require_bug_read

            require_bug_read(
                conn, bug, actor=read_source["actor"], grant_id=read_source["grant_id"]
            )
        old = conn.execute(
            "SELECT * FROM project_bug_snapshots WHERE bug_id=? AND observation_id=?",
            (bug_id, observation_id),
        ).fetchone()
        if old:
            if old["payload_digest"] != signature:
                raise BugConflict("observation ID reused for different content")
            return dict(old)
        if bug["snapshot_sequence"] != expected_sequence:
            raise BugConflict("newer observation exists; read again")
        last = conn.execute(
            "SELECT observed_at FROM project_bug_snapshots WHERE bug_id=? ORDER BY sequence DESC LIMIT 1",
            (bug_id,),
        ).fetchone()
        if last and when < parse_iso(last["observed_at"]):
            raise BugConflict("observation is older than current snapshot")
        snapshot_id = new_id("pbs")
        conn.execute(
            "INSERT INTO project_bug_snapshots VALUES(?,?,?,?,?,?,?,?)",
            (
                snapshot_id,
                bug_id,
                expected_sequence + 1,
                observation_id,
                signature,
                canonical_json(payload),
                observed_at,
                iso_now(),
            ),
        )
        conn.execute(
            "UPDATE project_bugs SET snapshot_sequence=snapshot_sequence+1,revision=revision+1 WHERE bug_id=?",
            (bug_id,),
        )
        if read_source is not None:
            conn.execute(
                "INSERT INTO project_bug_read_evidence VALUES(?,?,?,?)",
                (
                    snapshot_id,
                    read_source["actor"],
                    read_source["grant_id"],
                    canonical_json(read_source["evidence"]),
                ),
            )
        _event(
            conn, bug_id, "project_adapter", "observed", {"snapshot_id": snapshot_id}
        )
        return dict(
            conn.execute(
                "SELECT * FROM project_bug_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
        )


def start_round(conn, *, bug_id, actor, request_id, reason, expected_revision):
    """Open a new local investigation; never performs a remote reopen transition."""
    for label, value, limit in (
        ("actor", actor, 256),
        ("request ID", request_id, 256),
        ("reason", reason, 4000),
    ):
        _text(value, label, limit)
    signature = digest({"reason": reason, "expected_revision": expected_revision})
    with transaction(conn):
        bug = _bug(conn, bug_id)
        old = conn.execute(
            "SELECT * FROM project_bug_rounds WHERE bug_id=? AND actor=? AND request_id=?",
            (bug_id, actor, request_id),
        ).fetchone()
        if old:
            if old["request_digest"] != signature:
                raise BugConflict("request ID reused for different content")
            return dict(old)
        _revision(bug, expected_revision)
        if conn.execute(
            "SELECT 1 FROM project_bug_operations WHERE bug_id=? AND state IN ('prepared','dispatched','unknown')",
            (bug_id,),
        ).fetchone():
            raise BugConflict(
                "Bug has an unsettled write; reconcile before starting another round"
            )
        active = conn.execute(
            "SELECT * FROM project_bug_rounds WHERE bug_id=? AND archived_at IS NULL",
            (bug_id,),
        ).fetchone()
        unstarted = active and active["execution_state"] == "planned" and not conn.execute(
            "SELECT 1 FROM project_investigation_jobs WHERE round_id=?",
            (active["round_id"],),
        ).fetchone()
        if active and not unstarted and active["execution_state"] not in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            raise BugConflict("current investigation is not settled")
        if active:
            from .project_verification_runs import unsettled

            if unsettled(conn, active["round_id"]):
                raise BugConflict("verification execution remains unsettled")
            from .project_round_readiness import require

            require(conn, active["round_id"])
            conn.execute(
                "UPDATE project_bug_rounds SET archived_at=? WHERE round_id=?",
                (iso_now(), active["round_id"]),
            )
        number = conn.execute(
            "SELECT COALESCE(MAX(number),0)+1 FROM project_bug_rounds WHERE bug_id=?",
            (bug_id,),
        ).fetchone()[0]
        round_id = new_id("pbr")
        conn.execute(
            """INSERT INTO project_bug_rounds
            (round_id,bug_id,number,reason,request_id,request_digest,actor,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (round_id, bug_id, number, reason, request_id, signature, actor, iso_now()),
        )
        conn.execute(
            "UPDATE project_bugs SET revision=revision+1 WHERE bug_id=?", (bug_id,)
        )
        _event(conn, bug_id, actor, "round_started", {"reason": reason}, round_id)
        return dict(
            conn.execute(
                "SELECT * FROM project_bug_rounds WHERE round_id=?", (round_id,)
            ).fetchone()
        )


def detail(conn, bug_id):
    """Consistent local projection; no network, model call or state transition."""
    with transaction(conn, immediate=False):
        bug = dict(_bug(conn, bug_id))
        snapshot = conn.execute(
            "SELECT * FROM project_bug_snapshots WHERE bug_id=? ORDER BY sequence DESC LIMIT 1",
            (bug_id,),
        ).fetchone()
        bug["snapshot"] = (
            None
            if snapshot is None
            else {
                "snapshot_id": snapshot["snapshot_id"],
                "observed_at": snapshot["observed_at"],
                "sequence": snapshot["sequence"],
                **json.loads(snapshot["payload_json"]),
            }
        )
        if snapshot is not None:
            evidence = conn.execute(
                "SELECT evidence_json FROM project_bug_read_evidence WHERE snapshot_id=?",
                (snapshot["snapshot_id"],),
            ).fetchone()
            if evidence:
                bug["snapshot"]["read_evidence"] = json.loads(evidence["evidence_json"])
        bug["rounds"] = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM project_bug_rounds WHERE bug_id=? ORDER BY number",
                (bug_id,),
            )
        ]
        from .project_verification import current

        for round_ in bug["rounds"]:
            plan = current(conn, round_["round_id"])
            if plan is not None:
                round_["verification_state"] = plan["verification_state"]
            if round_["archived_at"] is None:
                from .project_round_readiness import inspect

                round_["settlement"] = inspect(conn, round_["round_id"])
        return bug
