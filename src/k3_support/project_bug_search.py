"""Actor-bound, explicitly scoped, leased official Project Bug search queue."""

import json
import sqlite3
import time
from datetime import timedelta

from . import project_bugs as bugs
from .db import transaction
from .ids import canonical_json, digest, new_id
from .project_bug_query import compile_query, validate_spaces
from .project_link_intake import _resolve_space
from .project_read_client import MeegleReadClient, ProjectReadError
from .project_reader_config import selected
from .project_refresh import RefreshBlocked, _fingerprint, _selection
from .timeutil import iso_now, parse_iso, utc_now


def _scope(config, simple_name, type_key):
    for scope in validate_spaces(
        config.raw.get("project_integration", {}).get("search_spaces", [])
    ):
        if scope["simple_name"] == simple_name and scope["type_key"] == type_key:
            return scope
    raise PermissionError("Project search scope is not configured")


def _reader(conn, config, actor):
    reader = selected(config)
    if reader is None:
        raise RefreshBlocked("search_unavailable")
    return _selection(conn, config, {"host": reader["host"]}, actor)


def _stamp(conn, config, reader, scope):
    return digest({"reader": _fingerprint(conn, config, reader), "scope": scope})


def projection(row):
    scope = json.loads(row["scope_json"])
    return {
        k: row[k]
        for k in (
            "search_id",
            "keyword",
            "state",
            "attempt",
            "error_code",
            "expires_at",
            "created_at",
            "updated_at",
        )
    } | {
        "scope": {k: scope[k] for k in ("simple_name", "type_key")},
        "authorization_expired": parse_iso(row["expires_at"]) <= utc_now(),
        "lease_expired": row["state"] == "running"
        and parse_iso(row["lease_expires_at"]) <= utc_now(),
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
    }


def _get(conn, actor, search_id):
    bugs._text(search_id, "search ID", 256)
    row = conn.execute(
        "SELECT * FROM project_search_requests WHERE search_id=? AND actor=?",
        (search_id, actor),
    ).fetchone()
    if row is None:
        raise ValueError("search not found")
    return row


def status(conn, *, actor, search_id):
    return projection(_get(conn, actor, search_id))


def options(conn, config, *, actor):
    scopes = validate_spaces(
        config.raw.get("project_integration", {}).get("search_spaces", [])
    )
    try:
        _reader(conn, config, actor)
        available = bool(scopes)
    except RefreshBlocked:
        available = False
    return {
        "available": available,
        "scopes": [
            {
                "simple_name": s["simple_name"],
                "type_key": s["type_key"],
                "item_limit": len(s["allowed_item_ids"])
                if s["allowed_item_ids"] is not None
                else None,
            }
            for s in scopes
        ],
        "history": [
            projection(r)
            for r in conn.execute(
                "SELECT * FROM project_search_requests WHERE actor=? ORDER BY rowid DESC LIMIT 20",
                (actor,),
            )
        ],
        "history_limit": 20,
    }


def _replay(conn, actor, request_id, signature):
    bugs._text(request_id, "search request", 256)
    row = conn.execute(
        "SELECT * FROM project_search_requests WHERE actor=? AND request_id=?",
        (actor, request_id),
    ).fetchone()
    if row and row["request_digest"] != signature:
        raise bugs.BugConflict("search request reused for different intent")
    return projection(row) if row else None


def _insert(
    conn,
    config,
    *,
    actor,
    request_id,
    signature,
    scope,
    keyword,
    after_id,
    expires_at,
    expected_stamp=None,
):
    compile_query(scope, keyword=keyword, after_id=after_id)
    reader = _reader(conn, config, actor)
    stamp = _stamp(conn, config, reader, scope)
    if parse_iso(expires_at) <= utc_now() or (
        expected_stamp is not None and stamp != expected_stamp
    ):
        raise PermissionError("search authorization changed")
    if (
        conn.execute(
            "SELECT count(*) FROM project_search_requests WHERE actor=? AND state IN ('queued','running') AND expires_at>?",
            (actor, iso_now()),
        ).fetchone()[0]
        >= 5
    ):
        raise bugs.BugConflict("too many pending searches")
    key, now = new_id("search"), iso_now()
    conn.execute(
        "INSERT INTO project_search_requests(search_id,actor,request_id,request_digest,scope_json,keyword,after_id,reader_digest,expires_at,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)",
        (
            key,
            actor,
            request_id,
            signature,
            canonical_json(scope),
            keyword,
            after_id,
            stamp,
            expires_at,
            now,
            now,
        ),
    )
    return status(conn, actor=actor, search_id=key)


def enqueue(conn, config, *, actor, simple_name, type_key, keyword, request_id):
    signature = digest(
        {"simple_name": simple_name, "type_key": type_key, "keyword": keyword}
    )
    with transaction(conn):
        old = _replay(conn, actor, request_id, signature)
        if old:
            return old
        return _insert(
            conn,
            config,
            actor=actor,
            request_id=request_id,
            signature=signature,
            scope=_scope(config, simple_name, type_key),
            keyword=keyword,
            after_id=0,
            expires_at=(utc_now() + timedelta(minutes=30)).isoformat(),
        )


def next_page(conn, config, *, actor, search_id, request_id):
    signature = digest({"previous_search_id": search_id})
    with transaction(conn):
        old = _replay(conn, actor, request_id, signature)
        if old:
            return old
        previous = _get(conn, actor, search_id)
        result = (
            json.loads(previous["result_json"]) if previous["result_json"] else None
        )
        if (
            previous["state"] != "succeeded"
            or not result
            or result["next_after_id"] is None
        ):
            raise ValueError("search has no next page")
        scope = json.loads(previous["scope_json"])
        return _insert(
            conn,
            config,
            actor=actor,
            request_id=request_id,
            signature=signature,
            scope=_scope(config, scope["simple_name"], scope["type_key"]),
            keyword=previous["keyword"],
            after_id=result["next_after_id"],
            expires_at=previous["expires_at"],
            expected_stamp=previous["reader_digest"],
        )


def run_one(conn, config_loader, *, client_factory=None, stop_event=None):
    if stop_event is not None and stop_event.is_set():
        return {"state": "stopped"}
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM project_search_requests WHERE state='queued' OR (state='running' AND lease_expires_at<=?) ORDER BY created_at,search_id LIMIT 1",
            (iso_now(),),
        ).fetchone()
        if row is None:
            return {"state": "idle"}
        if row["attempt"] >= 3:
            conn.execute(
                "UPDATE project_search_requests SET state='failed',error_code='interrupted_read_limit',lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE search_id=?",
                (iso_now(), row["search_id"]),
            )
            return status(conn, actor=row["actor"], search_id=row["search_id"])
        token = new_id("searchlease")
        conn.execute(
            "UPDATE project_search_requests SET state='running',attempt=attempt+1,lease_token=?,lease_expires_at=?,updated_at=? WHERE search_id=?",
            (
                token,
                (utc_now() + timedelta(seconds=90)).isoformat(),
                iso_now(),
                row["search_id"],
            ),
        )
    scope = json.loads(row["scope_json"])
    deadline = time.monotonic() + 300

    def guard():
        current = _get(conn, row["actor"], row["search_id"])
        if (
            current["state"] != "running"
            or current["lease_token"] != token
            or parse_iso(current["lease_expires_at"]) <= utc_now()
            or parse_iso(current["expires_at"]) <= utc_now()
            or time.monotonic() >= deadline
            or (stop_event is not None and stop_event.is_set())
        ):
            raise RefreshBlocked("search_authorization_changed")
        config = config_loader()
        reader = _reader(conn, config, row["actor"])
        live = _scope(config, scope["simple_name"], scope["type_key"])
        if (
            _stamp(conn, config, reader, live) != row["reader_digest"]
            or time.monotonic() >= deadline
        ):
            raise RefreshBlocked("search_configuration_changed")
        changed = conn.execute(
            "UPDATE project_search_requests SET lease_expires_at=?,updated_at=? WHERE search_id=? AND state='running' AND lease_token=? AND lease_expires_at>? AND expires_at>?",
            (
                (utc_now() + timedelta(seconds=90)).isoformat(),
                iso_now(),
                row["search_id"],
                token,
                iso_now(),
                iso_now(),
            ),
        )
        if changed.rowcount != 1:
            raise RefreshBlocked("search_lease_lost")
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
        _resolve_space(client, scope | {"host": reader["host"]}, guard)
        guard()
        result = client.query_bugs(
            scope, keyword=row["keyword"], after_id=row["after_id"]
        )
        if result.get("host") != reader["host"]:
            raise ProjectReadError("query_host_mismatch")
        for item in result["items"]:
            item["url"] = (
                f"https://{reader['host']}/{scope['simple_name']}/{scope.get('url_type_key', scope['type_key'])}/detail/{item['item_id']}"
            )
        with transaction(conn):
            guard()
            conn.execute(
                "UPDATE project_search_requests SET state='succeeded',result_json=?,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE search_id=? AND lease_token=?",
                (canonical_json(result), iso_now(), row["search_id"], token),
            )
        return status(conn, actor=row["actor"], search_id=row["search_id"])
    except (RefreshBlocked, PermissionError):
        state, error = "blocked", "search_authorization_changed"
    except ProjectReadError as exc:
        state, error = (
            "failed",
            "login_required"
            if exc.code in {"auth_login_required", "auth_rejected"}
            else "search_read_failed",
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        state, error = "failed", "search_unavailable"
    with transaction(conn):
        conn.execute(
            "UPDATE project_search_requests SET state=?,error_code=?,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE search_id=? AND state='running' AND lease_token=?",
            (state, error, iso_now(), row["search_id"], token),
        )
    return status(conn, actor=row["actor"], search_id=row["search_id"])
