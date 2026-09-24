"""Batch deferred owner notices; only a durable summary receipt retires them."""

import json

from .coordination import suppress_outbox, validate_outbox_fence
from .db import transaction
from .ids import digest, new_id
from .notification_snooze import status
from .store import enqueue_outbox
from .timeutil import iso_now


def cutoff(conn, *, config=None, now=None):
    row = conn.execute("SELECT * FROM notification_snooze WHERE singleton=1").fetchone()
    manual = (row["until_at"] or row["updated_at"]) if row else None
    night = status(conn, config=config, now=now)["night_cutoff"]
    candidates = [value for value in (manual, night) if value]
    return max(candidates) if candidates else None


def _binding(row):
    return digest(
        {
            key: row.get(key)
            for key in (
                "outbox_id",
                "case_id",
                "channel",
                "action_type",
                "destination",
                "payload_json",
                "turn_id",
                "turn_revision",
                "communication_fence",
                "global_outbound_fence",
                "context_id",
                "context_revision",
                "context_digest",
            )
        }
    )


def prepare(conn, config):
    """Called before claiming outbound work, once per worker iteration."""
    if status(conn, config=config)["active"] or not cutoff(conn, config=config):
        return None
    with transaction(conn):
        # Release only definitively unsent summaries. A timed-out/dispatched
        # summary retains its members for receipt-based recovery, never retry.
        conn.execute("""UPDATE notification_digest_members SET active=0 WHERE active=1
            AND digest_outbox_id IN (SELECT outbox_id FROM outbox
              WHERE state IN ('cancelled','permanent_failure') AND dispatch_started_at IS NULL)""")
        rows = conn.execute(
            """SELECT o.* FROM outbox o JOIN cases c ON c.case_id=o.case_id
            WHERE o.channel IN ('telegram','feishu_im') AND o.action_type='owner_decision'
              AND o.state IN ('pending','retry') AND o.created_at<=?
              AND (o.not_before IS NULL OR o.not_before<=?)
              AND (o.next_attempt_at IS NULL OR o.next_attempt_at<=?)
              AND c.severity IN ('P2','P3')
              AND NOT EXISTS (SELECT 1 FROM notification_digest_members m
                              WHERE m.original_outbox_id=o.outbox_id AND m.active=1)
            ORDER BY o.created_at,o.outbox_id LIMIT 200""",
            (
                cutoff(conn, config=config),
                iso_now(),
                iso_now(),
            ),
        ).fetchall()
        # Never combine recipients/channels into one receipt. Existing queued
        # notices retain their original destination after a config change.
        if rows:
            target = (rows[0]["channel"], rows[0]["destination"])
            rows = [row for row in rows if (row["channel"], row["destination"]) == target]
        valid = []
        for raw in rows:
            row = dict(raw)
            allowed, reason = validate_outbox_fence(conn, row)
            if not allowed:
                suppress_outbox(conn, outbox_id=row["outbox_id"], reason=str(reason))
            else:
                valid.append(row)
        if not valid:
            return None
        cases = list(dict.fromkeys(row["case_id"] for row in valid))
        text = f"普通待判断提醒汇总：{len(valid)} 条提醒，涉及 {len(cases)} 个事项。\n任务尚未处理，请在工作台查看原问题并接管或作出决定。\n"
        text += "\n".join(cases[:20])
        if len(cases) > 20:
            text += f"\n另有 {len(cases) - 20} 个事项，请查看工作台。"
        text += "\n使用 /feishu 打开控制菜单。"
        identifier, _ = enqueue_outbox(
            conn,
            channel=valid[0]["channel"],
            action_type="owner_digest",
            destination=valid[0]["destination"],
            payload={
                "text": text,
                "notice_count": len(valid),
                "case_count": len(cases),
            },
            idempotency_key=new_id("ordinary-digest"),
        )
        conn.executemany(
            "INSERT INTO notification_digest_members VALUES(?,?,?,1)",
            [(identifier, row["outbox_id"], _binding(row)) for row in valid],
        )
        summary = dict(
            conn.execute(
                "SELECT * FROM outbox WHERE outbox_id=?", (identifier,)
            ).fetchone()
        )
        conn.execute(
            "INSERT INTO notification_digest_batches VALUES(?,?)",
            (identifier, _binding(summary)),
        )
    return identifier


def valid(conn, row):
    batch = conn.execute(
        "SELECT summary_digest FROM notification_digest_batches WHERE digest_outbox_id=?",
        (row["outbox_id"],),
    ).fetchone()
    if batch is None or batch[0] != _binding(row):
        return False
    members = conn.execute(
        "SELECT * FROM notification_digest_members WHERE digest_outbox_id=? AND active=1",
        (row["outbox_id"],),
    ).fetchall()
    if not members:
        return False
    for member in members:
        original = conn.execute(
            "SELECT o.*,c.severity FROM outbox o JOIN cases c ON c.case_id=o.case_id WHERE o.outbox_id=?",
            (member["original_outbox_id"],),
        ).fetchone()
        if not original:
            return False
        original = dict(original)
        if original["state"] not in {"pending", "retry"} or original[
            "severity"
        ] not in {"P2", "P3"}:
            return False
        if (
            _binding(original) != member["original_digest"]
            or not validate_outbox_fence(conn, original)[0]
        ):
            return False
    payload = json.loads(row["payload_json"])
    return payload.get("notice_count") == len(members)


def finalize(conn, row):
    """Inside the durable-delivery transaction; do not mutate Case state."""
    members = conn.execute(
        "SELECT * FROM notification_digest_members WHERE digest_outbox_id=? AND active=1",
        (row["outbox_id"],),
    ).fetchall()
    for member in members:
        original = conn.execute(
            "SELECT o.*,c.severity FROM outbox o JOIN cases c ON c.case_id=o.case_id WHERE o.outbox_id=?",
            (member["original_outbox_id"],),
        ).fetchone()
        if (
            original
            and original["severity"] in {"P2", "P3"}
            and original["state"] in {"pending", "retry"}
            and _binding(dict(original)) == member["original_digest"]
        ):
            suppress_outbox(
                conn,
                outbox_id=original["outbox_id"],
                reason="ordinary_notice_covered_by_digest:" + row["outbox_id"],
            )
    conn.execute(
        "UPDATE notification_digest_members SET active=0 WHERE digest_outbox_id=?",
        (row["outbox_id"],),
    )
