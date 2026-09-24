"""Body-retention dependency preview; not deletion authorization."""

import json
import time
from datetime import UTC, datetime, timedelta

from .retention_transaction import bounded_transaction
from .ids import digest
from .timeutil import iso_now


def _identifier(name):
    return '"' + name.replace('"', '""') + '"'


def _json_dependencies(conn, columns, events, *, max_records=100_000, max_bytes=64 * 1024 * 1024,
                       max_nodes=1_000_000):
    if (type(max_records) is not int or max_records < 1 or type(max_bytes) is not int or max_bytes < 1
            or type(max_nodes) is not int or max_nodes < 1):
        raise ValueError('dependency scan budgets must be positive integers')
    scanned_records = scanned_bytes = scanned_nodes = 0
    deadline = time.monotonic() + 10
    found = {event["event_pk"]: [] for event in events}
    issues = []
    targets = {}
    for event in events:
        for key in (event["event_pk"], event["external_id"]):
            targets.setdefault(key, set()).add(event["event_pk"])
    if not targets:
        return found, issues
    for table, column in columns:
        counts, invalid = {}, 0
        owner = {'inbound_events': 'event_pk', 'knowledge_authoring_drafts': 'candidate_id'}.get(table, 'NULL')
        # Stream each column once per page, never once per target event. Match
        # JSON string values and map keys, deduplicating within the owning row.
        # ID-indexed maps can hold references in keys rather than values.
        for record in conn.execute(f"SELECT {owner},{_identifier(column)} FROM {_identifier(table)}"):
            scanned_records += 1
            value_bytes = len(record[1].encode('utf-8')) if isinstance(record[1], str) else 0
            scanned_bytes += value_bytes
            if scanned_records > max_records or scanned_bytes > max_bytes or time.monotonic() >= deadline:
                issues.append({'table': table, 'column': column, 'scan_incomplete': True,
                               'reason': 'dependency_scan_budget_exhausted'})
                return found, issues
            if record[1] is None:
                continue
            try:
                # Preserve duplicate object keys: normal dict decoding can hide
                # an earlier reference behind a later value with the same key.
                pending = [json.loads(record[1], object_pairs_hook=lambda pairs: [list(pair) for pair in pairs])]
            except (ValueError, TypeError, RecursionError):
                invalid += 1
                continue
            matches = set()
            while pending:
                scanned_nodes += 1
                if scanned_nodes > max_nodes or time.monotonic() >= deadline:
                    issues.append({'table': table, 'column': column, 'scan_incomplete': True,
                                   'reason': 'dependency_scan_budget_exhausted'})
                    return found, issues
                value = pending.pop()
                if isinstance(value, str):
                    matches.update(targets.get(value, ()))
                elif isinstance(value, dict):
                    pending.extend(value.keys())
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
            matches.discard(record[0])
            for key in matches:
                counts[key] = counts.get(key, 0) + 1
        for key, count in counts.items():
            found[key].append({"table": table, "column": column, "count": count, "kind": "json_id_reference"})
        if invalid:
            issues.append({"table": table, "column": column, "unreadable_records": invalid})
    return found, issues


def preview(conn, *, days, after_id="", limit=30, now=None):
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError("正文保留天数必须为 1–3650")
    if not isinstance(after_id, str) or len(after_id) > 100 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("无效清理预览分页")
    clock = now or datetime.now(UTC)
    if clock.tzinfo is None:
        raise ValueError("清理预览时间必须包含时区")
    cutoff = int((clock - timedelta(days=days)).timestamp())
    conn.execute("SAVEPOINT body_retention_preview")
    try:
        # Discover actual foreign keys so newer dependencies are visible without
        # duplicating an ever-growing table allowlist. This is not a completeness
        # claim for references embedded in arbitrary text, files or external systems.
        references, json_columns = [], []
        for table in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for column in conn.execute(f"PRAGMA table_info({_identifier(table[0])})"):
                if column[1].endswith("_json"):
                    json_columns.append((table[0], column[1]))
            for fk in conn.execute(f"PRAGMA foreign_key_list({_identifier(table[0])})"):
                if fk[2] == "inbound_events" and fk[4] == "event_pk":
                    references.append((table[0], fk[3]))
        rows = conn.execute(
            "SELECT * FROM inbound_events WHERE received_epoch<? AND event_pk>? ORDER BY event_pk LIMIT ?",
            (cutoff, after_id, limit + 1),
        ).fetchall()
        indexed = conn.execute('SELECT 1 FROM retention_reference_generations LIMIT 1').fetchone() is not None
        if indexed:
            from .retention_reference_query import dependencies as indexed_dependencies
            json_dependencies, issues = indexed_dependencies(conn, rows[:limit])
        else:
            json_dependencies, issues = _json_dependencies(conn, json_columns, rows[:limit])
        items = []
        for row in rows[:limit]:
            dependencies = []
            for table, column in ([] if indexed else references):
                count = conn.execute(
                    f"SELECT count(*) FROM {_identifier(table)} WHERE {_identifier(column)}=?", (row["event_pk"],)
                ).fetchone()[0]
                if count:
                    dependencies.append({"table": table, "column": column, "count": count})
            # Source registries use stable external IDs rather than foreign keys.
            # Conservatively retain collisions across identities; a missing identity
            # discriminator must never make an existing reference disappear.
            for table in (() if indexed else ("case_sources", "knowledge_sources")):
                count = conn.execute(
                    f"SELECT count(*) FROM {_identifier(table)} WHERE stable_external_id IN (?,?)",
                    (row["event_pk"], row["external_id"]),
                ).fetchone()[0]
                if count:
                    dependencies.append({"table": table, "column": "stable_external_id", "count": count})
            dependencies.extend(json_dependencies[row["event_pk"]])
            dependencies.sort(key=lambda item: (item["table"], item["column"]))
            receipt = conn.execute('''SELECT cleared_at,actor,retention_days,body_bytes,last_error_digest,last_error_bytes
                FROM body_retention_receipts WHERE event_pk=?''', (row['event_pk'],)).fetchone()
            blockers = []
            if receipt:
                blockers.append('already_cleared')
            if dependencies:
                blockers.append('referenced')
            if issues:
                blockers.append('dependency_scan_incomplete')
            if row['status'] not in {'processed', 'ignored'}:
                blockers.append('nonterminal')
            if row['lease_owner'] or row['lease_expires_at']:
                blockers.append('lease_present')
            if row['raw_artifact_path']:
                blockers.append('raw_artifact_present')
            items.append({"event_pk": row["event_pk"], "source": row["source"],
                          "received_at": row["received_at"], "status": row["status"],
                          "body_bytes": len(row["payload_json"].encode()),
                          "last_error_bytes": len((row['last_error'] or '').encode()),
                          "body_state": 'cleared_by_retention' if receipt else 'stored',
                          "clear_receipt": dict(receipt) if receipt else None,
                          "clear_blockers": blockers,
                          "dependencies": dependencies,
                          "processing_active": row["status"] in ("new", "claimed"),
                          "snapshot_digest": digest({"event": dict(row), "dependencies": dependencies,
                                                     "retention_days": days,
                                                     "fields_policy": 'payload-and-error-v2'}), "deletion_allowed": False})
        return {"items": items, "next_cursor": rows[limit - 1]["event_pk"] if len(rows) > limit else None,
                "cutoff_epoch": cutoff, "retention_days": days, "read_only": True,
                "json_scan_issues": issues,
                "scope": "数据库外键、来源登记及 JSON 精确 ID 引用预览；未覆盖嵌入文本的 ID、派生副本和备份，不构成删除授权。"}
    finally:
        conn.execute("RELEASE body_retention_preview")


def clear_unreferenced_page(conn, *, days, expected, actor, after_id='', limit=30, now=None, guard=None):
    """Internal explicit apply; not a policy scheduler or authorization endpoint.

    Clear selected terminal, unreferenced payloads and error details. Identity rows
    remain for ingress deduplication. Files, replicas, backups and SQLite free
    pages are not erased by this operation.
    """
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
        raise ValueError('retention actor is required')
    if not isinstance(expected, dict) or not 1 <= len(expected) <= 50:
        raise ValueError('explicit preview selection is required')
    if any(not isinstance(key, str) or not isinstance(value, str) or len(value) != 64
           for key, value in expected.items()):
        raise ValueError('invalid retention preview binding')
    with bounded_transaction(conn):
        if guard is not None and not guard():
            raise ValueError('retention guard no longer permits clearing')
        current = preview(conn, days=days, after_id=after_id, limit=limit, now=now)
        if current['json_scan_issues']:
            raise ValueError('dependency scan is incomplete or unreadable')
        candidates = {item['event_pk']: item for item in current['items']}
        selected = []
        for event_pk, expected_digest in expected.items():
            item = candidates.get(event_pk)
            if item is None or item['snapshot_digest'] != expected_digest:
                raise ValueError('retention preview changed; create a fresh preview')
            row = conn.execute('SELECT * FROM inbound_events WHERE event_pk=?', (event_pk,)).fetchone()
            if item['clear_blockers']:
                raise ValueError('retention target is referenced, active or has a raw artifact')
            selected.append((item, row))
        for item, row in selected:
            conn.execute('''INSERT INTO body_retention_receipts
                (event_pk,preview_digest,payload_digest,body_bytes,retention_days,actor,cleared_at,last_error_digest,last_error_bytes)
                VALUES(?,?,?,?,?,?,?,?,?)''', (row['event_pk'], item['snapshot_digest'],
                digest(row['payload_json']), item['body_bytes'], days, actor, iso_now(),
                digest(row['last_error']), item['last_error_bytes']))
            conn.execute("UPDATE inbound_events SET payload_json='{}',last_error=NULL WHERE event_pk=?", (row['event_pk'],))
    return {'cleared': len(selected), 'body_bytes': sum(item['body_bytes'] for item, _ in selected),
            'last_error_bytes': sum(item['last_error_bytes'] for item, _ in selected),
            'scope': 'database_payload_and_error_not_files_backups_or_secure_erasure'}
