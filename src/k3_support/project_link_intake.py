"""Exact-link read authorization and atomic local Bug intake. No remote creation."""

import json
import sqlite3
import time
from datetime import timedelta

from . import project_bug_grants as grants
from . import project_bugs as bugs
from .db import transaction
from .ids import canonical_json, digest, new_id
from .project_intake_policy import parse, validate
from .project_read_client import MeegleReadClient, ProjectReadError
from .project_read_snapshot import SnapshotReader
from .project_reader_config import selected
from .project_refresh import RefreshBlocked, _fingerprint, _selection
from .store import create_case
from .timeutil import iso_now, parse_iso, utc_now


def projection(row):
    result = {
        k: row[k]
        for k in (
            "intake_id",
            "url",
            "state",
            "attempt",
            "bug_id",
            "grant_id",
            "snapshot_id",
            "reused",
            "error_code",
            "local_priority",
            "grant_expires_at",
            "created_at",
            "updated_at",
        )
    }
    result["lease_expired"] = (
        row["state"] == "running" and parse_iso(row["lease_expires_at"]) <= utc_now()
    )
    result["authorization_expired"] = (
        row["state"] in {"queued", "running"}
        and parse_iso(row["expires_at"]) <= utc_now()
    )
    return result


def listing(conn, config, *, actor, after_id):
    if not isinstance(after_id, str) or len(after_id) > 256:
        raise ValueError("invalid intake cursor")
    rows = conn.execute(
        "SELECT * FROM project_link_intakes WHERE actor=? AND intake_id>? ORDER BY intake_id LIMIT 21",
        (actor, after_id),
    ).fetchall()
    reader = selected(config)
    spaces = validate(
        config.raw.get("project_integration", {}).get("intake_spaces", [])
    )
    available = False
    if reader and spaces:
        try:
            _selection(conn, config, {"host": reader["host"]}, actor)
            available = True
        except RefreshBlocked:
            pass
    return {
        "available": available,
        "items": [projection(r) for r in rows[:20]],
        "next_cursor": rows[19]["intake_id"] if len(rows) > 20 else None,
    }


def status(conn, *, actor, intake_id):
    row = conn.execute(
        "SELECT * FROM project_link_intakes WHERE intake_id=? AND actor=?",
        (intake_id, actor),
    ).fetchone()
    if row is None:
        raise ValueError("intake not found")
    return projection(row)


def enqueue(conn, config, *, actor, url, request_id, read_hours, local_priority):
    bugs._text(request_id, "intake request", 256)
    if (
        type(read_hours) is not int
        or read_hours not in {1, 8, 24}
        or not isinstance(local_priority, str)
        or local_priority not in {"P0", "P1", "P2", "P3"}
    ):
        raise ValueError("invalid intake authorization or local priority")
    signature = digest(
        {"url": url, "read_hours": read_hours, "local_priority": local_priority}
    )
    with transaction(conn):
        if request_id.startswith("chat-") and conn.execute(
            "SELECT 1 FROM project_bug_create_drafts WHERE actor=? AND request_id=?",
            (actor, request_id),
        ).fetchone():
            raise bugs.BugConflict("native message ID already belongs to a create draft")
        old = conn.execute(
            "SELECT * FROM project_link_intakes WHERE actor=? AND request_id=?",
            (actor, request_id),
        ).fetchone()
        if old:
            if old["request_digest"] != signature:
                raise bugs.BugConflict("intake request reused for different intent")
            return projection(old)
        identity = parse(config, url)
        reader = _selection(conn, config, identity, actor)
        active = conn.execute(
            "SELECT * FROM project_link_intakes WHERE url=? AND state IN ('queued','running')",
            (url,),
        ).fetchone()
        if active:
            if parse_iso(active["expires_at"]) > utc_now():
                raise bugs.BugConflict("this link already has a pending intake")
            conn.execute(
                "UPDATE project_link_intakes SET state='blocked',error_code='intake_authorization_changed',lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE intake_id=?",
                (iso_now(), active["intake_id"]),
            )
        key, now = new_id("intake"), iso_now()
        conn.execute(
            "INSERT INTO project_link_intakes(intake_id,actor,request_id,request_digest,url,identity_json,reader_digest,expires_at,grant_expires_at,local_priority,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?)",
            (
                key,
                actor,
                request_id,
                signature,
                url,
                canonical_json(identity),
                digest(
                    {"reader": _fingerprint(conn, config, reader), "identity": identity}
                ),
                (utc_now() + timedelta(minutes=30)).isoformat(),
                (utc_now() + timedelta(hours=read_hours)).isoformat(),
                local_priority,
                now,
                now,
            ),
        )
        return status(conn, actor=actor, intake_id=key)


def _resolve(client, row, identity, guard):
    guard()
    decoded = client.decode_workitem_url(row["url"])
    if not isinstance(decoded, dict):
        raise ProjectReadError("intake_identity_mismatch")
    if any(
        decoded.get(k) != v
        for k, v in {
            "host": identity["host"],
            "simple_name": identity["simple_name"],
            "work_item_type": identity.get("url_type_key", identity["type_key"]),
            "work_item_id": identity["item_id"],
        }.items()
    ):
        raise ProjectReadError("intake_identity_mismatch")
    _resolve_space(client, identity, guard)


def _resolve_space(client, identity, guard):
    matches = []
    seen = set()
    total = None
    count = 0
    for number in range(1, 21):
        guard()
        result = client.read_page(
            "project.search",
            {"project_key": identity["project_key"], "page_num": number},
        )
        if (
            not isinstance(result, dict)
            or result.get("host") != identity["host"]
            or result.get("command") != "project.search"
        ):
            raise ProjectReadError("intake_identity_mismatch")
        data = result.get("payload", {})
        if not isinstance(data, dict):
            raise ProjectReadError("invalid_intake_space_response")
        page = data.get("pagination", {})
        projects = data.get("projects")
        if not isinstance(page, dict):
            raise ProjectReadError("invalid_intake_space_response")
        if (
            not isinstance(projects, list)
            or len(projects) > 50
            or type(page.get("has_more")) is not bool
            or type(page.get("page_num")) is not int
            or page["page_num"] != number
            or type(page.get("page_size")) is not int
            or page["page_size"] != 50
            or type(page.get("total")) is not int
            or not 0 <= page["total"] <= 1000
        ):
            raise ProjectReadError("invalid_intake_space_response")
        if total is None:
            total = page["total"]
        if total != page["total"]:
            raise ProjectReadError("intake_space_changed")
        count += len(projects)
        for project in projects:
            if (
                not isinstance(project, dict)
                or not isinstance(project.get("project_key"), str)
                or project["project_key"] in seen
            ):
                raise ProjectReadError("invalid_intake_space_response")
            seen.add(project["project_key"])
            if (
                project["project_key"] == identity["project_key"]
                and project.get("simple_name") == identity["simple_name"]
            ):
                matches.append(project)
        if not page["has_more"]:
            if count != total or len(matches) != 1:
                raise ProjectReadError("intake_identity_mismatch")
            return
        if len(projects) != 50 or count >= total:
            raise ProjectReadError("invalid_intake_space_response")
    raise ProjectReadError("intake_page_limit")


def run_one(conn, config_loader, *, client_factory=None, stop_event=None):
    if stop_event is not None and stop_event.is_set():
        return {"state": "stopped"}
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM project_link_intakes WHERE state='queued' OR (state='running' AND lease_expires_at<=?) ORDER BY created_at,intake_id LIMIT 1",
            (iso_now(),),
        ).fetchone()
        if row is None:
            return {"state": "idle"}
        if row["attempt"] >= 3:
            conn.execute(
                "UPDATE project_link_intakes SET state='failed',error_code='interrupted_read_limit',lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE intake_id=?",
                (iso_now(), row["intake_id"]),
            )
            return status(conn, actor=row["actor"], intake_id=row["intake_id"])
        token = new_id("intakelease")
        conn.execute(
            "UPDATE project_link_intakes SET state='running',attempt=attempt+1,lease_token=?,lease_expires_at=?,updated_at=? WHERE intake_id=?",
            (
                token,
                (utc_now() + timedelta(seconds=90)).isoformat(),
                iso_now(),
                row["intake_id"],
            ),
        )
        identity = json.loads(row["identity_json"])
        prior = conn.execute(
            "SELECT bug_id,snapshot_sequence FROM project_bugs WHERE host=? AND project_key=? AND type_key=? AND item_id=?",
            tuple(identity[k] for k in ("host", "project_key", "type_key", "item_id")),
        ).fetchone()
    deadline = time.monotonic() + 300

    def guard():
        current = conn.execute(
            "SELECT * FROM project_link_intakes WHERE intake_id=?", (row["intake_id"],)
        ).fetchone()
        if (
            current["state"] != "running"
            or current["lease_token"] != token
            or parse_iso(current["lease_expires_at"]) <= utc_now()
            or parse_iso(current["expires_at"]) <= utc_now()
            or time.monotonic() >= deadline
            or (stop_event is not None and stop_event.is_set())
        ):
            raise RefreshBlocked("intake_authority_expired")
        config = config_loader()
        live_identity = parse(config, row["url"])
        reader = _selection(conn, config, live_identity, row["actor"])
        if (
            digest(
                {
                    "reader": _fingerprint(conn, config, reader),
                    "identity": live_identity,
                }
            )
            != row["reader_digest"]
        ):
            raise RefreshBlocked("intake_configuration_changed")
        if time.monotonic() >= deadline:
            raise RefreshBlocked("intake_deadline_expired")
        changed = conn.execute(
            "UPDATE project_link_intakes SET lease_expires_at=?,updated_at=? WHERE intake_id=? AND state='running' AND lease_token=? AND lease_expires_at>? AND expires_at>?",
            (
                (utc_now() + timedelta(seconds=90)).isoformat(),
                iso_now(),
                row["intake_id"],
                token,
                iso_now(),
                iso_now(),
            ),
        )
        if changed.rowcount != 1:
            raise RefreshBlocked("intake_lease_lost")
        return reader

    try:
        reader = guard()
        client = (
            client_factory
            or (
                lambda r: MeegleReadClient(
                    **{k: v for k, v in r.items() if k != "enabled"}
                )
            )
        )(reader)
        _resolve(client, row, identity, guard)
        bundle = SnapshotReader(client, before_read=guard).collect(
            {k: identity[k] for k in ("host", "project_key", "type_key", "item_id")}
        )
        with transaction(conn):
            guard()
            existing = conn.execute(
                "SELECT * FROM project_bugs WHERE host=? AND project_key=? AND type_key=? AND item_id=?",
                tuple(
                    identity[k] for k in ("host", "project_key", "type_key", "item_id")
                ),
            ).fetchone()
            if existing and (
                prior is None
                or existing["bug_id"] != prior["bug_id"]
                or existing["snapshot_sequence"] != prior["snapshot_sequence"]
            ):
                raise bugs.BugConflict("newer Bug observation exists")
            if existing:
                bug = dict(existing)
            else:
                case_id, _ = create_case(
                    conn,
                    title=bundle["snapshot"]["fields"]["name"],
                    case_type="bug",
                    severity=row["local_priority"],
                    confidence=1,
                    disclosure_class="private",
                    idempotency_key="project-intake:" + row["intake_id"],
                )
                bug = bugs.bind(
                    conn,
                    case_id=case_id,
                    actor=row["actor"],
                    **{
                        k: identity[k]
                        for k in ("host", "project_key", "type_key", "item_id")
                    },
                )
            scope = {k: identity[k] for k in ("host", "project_key", "type_key")}
            scope.update(
                bug_ids=[bug["bug_id"]],
                actions=["bug.read"],
                fields=[],
                transitions=[],
                repositories=[],
                devices=[],
            )
            grant = grants.issue(
                conn,
                actor=row["actor"],
                request_id="intake:" + row["intake_id"],
                scope=scope,
                expires_at=row["grant_expires_at"],
            )
            snapshot = bugs.observe(
                conn,
                bug_id=bug["bug_id"],
                observation_id="intake:" + row["intake_id"],
                expected_sequence=bug["snapshot_sequence"],
                payload=bundle["snapshot"],
                observed_at=bundle["observed_at"],
                read_source={
                    "actor": row["actor"],
                    "grant_id": grant["grant_id"],
                    "evidence": {
                        "read_started_at": bundle["read_started_at"],
                        **bundle["read_evidence"],
                    },
                },
                before_record=guard,
            )
            conn.execute(
                "UPDATE project_link_intakes SET state='succeeded',bug_id=?,grant_id=?,snapshot_id=?,reused=?,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE intake_id=?",
                (
                    bug["bug_id"],
                    grant["grant_id"],
                    snapshot["snapshot_id"],
                    int(existing is not None),
                    iso_now(),
                    row["intake_id"],
                ),
            )
            bugs._event(
                conn,
                bug["bug_id"],
                row["actor"],
                "link_intake_completed",
                {
                    "intake_id": row["intake_id"],
                    "reused": existing is not None,
                    "local_priority_policy": "existing Case unchanged; new Case uses operator-selected queue priority",
                },
            )
        return status(conn, actor=row["actor"], intake_id=row["intake_id"])
    except (RefreshBlocked, PermissionError):
        state, error = "blocked", "intake_authorization_changed"
    except bugs.BugConflict:
        state, error = "failed", "newer_snapshot_or_conflict"
    except ProjectReadError as exc:
        state = "failed"
        error = (
            "login_required"
            if exc.code in {"auth_login_required", "auth_rejected"}
            else "intake_read_or_identity_failed"
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        state, error = "failed", "intake_unavailable"
    with transaction(conn):
        conn.execute(
            "UPDATE project_link_intakes SET state=?,error_code=?,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE intake_id=? AND state='running' AND lease_token=?",
            (state, error, iso_now(), row["intake_id"], token),
        )
    return status(conn, actor=row["actor"], intake_id=row["intake_id"])
