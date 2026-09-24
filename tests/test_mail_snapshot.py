from __future__ import annotations

import json
import sqlite3

import pytest
import yaml

from k3_support.ids import canonical_json
from k3_support.mail import (
    MailError,
    commit_summary_delivery,
    prepare_summary,
    upsert_mail_item,
)
from k3_support.mail_catalog import _store_item
from k3_support.mail_snapshot import (
    MailSnapshotError,
    classification_review_digest,
    correct_category,
    query_summary,
)
from k3_support.store import ingest_event


def item(conn, number, *, category="build_ci", attention="information", thread="build-thread", timestamp=1788231600000):
    message_id = f"mail-{number:03d}"
    payload = {"message_id": message_id, "thread_id": thread, "subject": f"Build {number}",
               "internal_date": str(timestamp), "body_preview": "PRIVATE BODY MUST NOT ENTER MEMBERSHIP",
               "head_from": {"name": "Gerrit", "mail_address": "gerrit@example.test"}}
    ingest_event(conn, source="feishu_mail", identity="user", external_id=message_id + ":received",
                 payload=payload, occurred_at="2026-09-01T03:00:00+00:00")
    upsert_mail_item(conn, payload)
    value = {"message_id": message_id, "thread_id": thread, "folder": "INBOX",
             "sender_name": "Gerrit", "sender_address": "gerrit@example.test", "subject": f"Build {number}",
             "internal_date": str(timestamp), "labels": []}
    classification = {"category": category, "attention": attention, "origin": "automation",
                      "topics": ["build_infra"], "confidence": 0.98}
    _store_item(conn, value, classification, now="2026-09-01T03:00:00+00:00")
    return message_id, value, classification


def prepare(conn, **kwargs):
    return prepare_summary(
        conn, scheduled_at="2026-09-01T12:00:00+08:00", destination="telegram:owner-chat", slot="mail_noon",
        summarizer=lambda value: {"overview": "构建为主", "categories": [
            {"category": "other", "count": len(value["messages"]), "summary": "故意不同的模型归类"}], "important": []},
        **kwargs,
    )


def test_snapshot_exact_categories_stable_pages_and_no_body(conn):
    for index in range(125):
        item(conn, index, category="build_ci" if index < 100 else "upstream",
             attention="blocked" if index == 10 else "information",
             thread="build-thread" if index < 100 else f"upstream-{index}")
    summary = prepare(conn)
    result = query_summary(conn, digest_id=summary["summary_id"], category="build_ci", page_size=33)
    assert result["historical_count"] == 125
    assert result["categories"] == [{"category": "build_ci", "count": 100}, {"category": "upstream", "count": 25}]
    assert result["message_count"] == 100 and result["thread_count"] == 1 and result["page_count"] == 4
    assert "构建与 CI（100）" in summary["text"] and "Upstream（25）" in summary["text"]
    assert "故意不同的模型归类" not in summary["text"]
    observed = []
    before = conn.total_changes
    for page in range(1, 5):
        page_result = query_summary(conn, digest_id=summary["summary_id"], category="build_ci", page=page,
                                    page_size=33, expected_digest=result["snapshot_digest"])
        observed.extend(member["message_id"] for member in page_result["items"])
    assert conn.total_changes == before
    assert len(observed) == len(set(observed)) == 100
    assert observed == sorted(observed)
    blocked = query_summary(conn, digest_id=summary["summary_id"], attention="blocked")
    assert blocked["message_count"] == 1 and blocked["items"][0]["message_id"] == "mail-010"
    metadata = "".join(row[0] for row in conn.execute("SELECT metadata_json FROM mail_summary_membership"))
    assert "PRIVATE BODY" not in metadata
    item(conn, 200, category="build_ci")
    assert query_summary(conn, digest_id=summary["summary_id"], category="build_ci")["message_count"] == 100


def test_correction_is_audited_does_not_rewrite_history_and_survives_scanner(conn):
    message_id, value, classification = item(conn, 1)
    summary = prepare(conn)
    request = {"message_id": message_id, "category": "company", "actor_id": "operator", "reason": "这是公司公告",
               "expected_updated_at": "2026-09-01T03:00:00+00:00", "external_id": "correction-1"}
    assert correct_category(conn, **request)["applied"]
    assert not correct_category(conn, **request)["applied"]
    with pytest.raises(MailSnapshotError, match="replay"):
        correct_category(conn, **{**request, "category": "upstream"})
    with pytest.raises(MailSnapshotError, match="changed"):
        correct_category(conn, **{**request, "external_id": "old-classification"})
    _store_item(conn, value, classification, now="2026-09-02T00:00:00+00:00")
    assert conn.execute("SELECT category,classification_source FROM mail_catalog_items").fetchone()[:] == ("company", "operator")
    historical = query_summary(conn, digest_id=summary["summary_id"])
    assert historical["items"][0]["category"] == "build_ci"
    assert historical["items"][0]["current_category"] == "company"
    assert historical["items"][0]["classification_changed_since_summary"]
    assert conn.execute("SELECT count(*) FROM mail_category_corrections").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE mail_summary_membership SET category='company'")


def test_snapshot_does_not_change_during_summarizer_and_timezone_is_instance_owned(conn):
    item(conn, 1)

    def summarizer(value):
        assert value["category_membership"] == [{"message_id": "mail-001", "category": "build_ci", "attention": "information"}]
        item(conn, 2)
        conn.execute("UPDATE mail_catalog_items SET category='company' WHERE message_id='mail-001'")
        return {"overview": "One build", "categories": [{"category": "build_ci", "count": 1, "summary": "One build"}], "important": []}

    summary = prepare_summary(conn, scheduled_at="2026-09-01T12:00:00+08:00", destination="telegram:owner-chat",
                              slot="mail_noon", summarizer=summarizer, timezone="Asia/Kathmandu")
    snapshot = query_summary(conn, digest_id=summary["summary_id"])
    assert snapshot["message_count"] == 1 and snapshot["categories"] == [{"category": "build_ci", "count": 1}]
    assert snapshot["items"][0]["current_category"] == "company"
    assert "2026-09-01 09:45" in summary["text"]


def test_historical_date_filter_is_half_open_and_requires_aware_bounds(conn):
    item(conn, 1, timestamp=1788231600000)
    item(conn, 2, timestamp=1788235200000)
    summary = prepare(conn)
    result = query_summary(conn, digest_id=summary["summary_id"], since="2026-09-01T03:00:00+00:00", until="2026-09-01T04:00:00+00:00")
    assert result["message_count"] == 1 and result["items"][0]["message_id"] == "mail-001"
    for kwargs in ({"since": "2026-09-01T00:00:00"}, {"page": 0}, {"page": 2}, {"category": "made-up"}, {"expected_digest": "wrong"}):
        with pytest.raises((MailSnapshotError, ValueError)):
            query_summary(conn, digest_id=summary["summary_id"], **kwargs)


def test_legacy_summary_has_no_invented_membership(conn):
    summary = prepare(conn)
    conn.execute("UPDATE mail_digest_runs SET membership_digest=NULL WHERE digest_id=?", (summary["summary_id"],))
    result = query_summary(conn, digest_id=summary["summary_id"])
    assert result["state"] == "legacy_membership_unavailable" and not result["items"]
    assert result["historical_count"] == 0


def test_cli_summary_show_reads_exact_snapshot(conn, config, capsys):
    from k3_support.cli import main

    item(conn, 1)
    summary = prepare(conn)
    config.path.write_text(yaml.safe_dump(config.raw), encoding="utf-8")
    assert main(["--config", str(config.path), "mail-summary-show", summary["summary_id"], "--category", "build_ci"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["message_count"] == 1 and output["read_only"]


def test_late_discovery_before_delivered_watermark_is_in_next_snapshot(conn):
    item(conn, 1)
    noon = prepare(conn)
    # This message has an old timestamp but arrived after the noon snapshot.
    item(conn, 2)
    conn.execute("UPDATE outbox SET state='delivered',remote_message_id='noon-receipt' WHERE outbox_id=?", (noon["outbox_id"],))
    commit_summary_delivery(conn, outbox_id=noon["outbox_id"], remote_message_id="noon-receipt")
    evening = prepare_summary(
        conn, scheduled_at="2026-09-01T18:00:00+08:00", destination="telegram:owner-chat", slot="mail_evening",
        summarizer=lambda value: {"overview": "补收邮件", "categories": [{"category": "build_ci", "count": len(value["messages"]), "summary": "补收"}], "important": []},
    )
    assert evening["item_count"] == 1
    expanded = query_summary(conn, digest_id=evening["summary_id"])
    assert [row["message_id"] for row in expanded["items"]] == ["mail-002"]
    assert query_summary(conn, digest_id=noon["summary_id"])["message_count"] == 1


def test_body_backfill_includes_old_late_arrival_but_not_frozen_members(conn):
    from k3_support.lark import CommandResult
    from k3_support.mail import refresh_missing_bodies

    item(conn, 1)
    noon = prepare(conn)
    conn.execute("UPDATE outbox SET state='delivered',remote_message_id='fixture-noon' WHERE outbox_id=?", (noon["outbox_id"],))
    commit_summary_delivery(conn, outbox_id=noon["outbox_id"], remote_message_id="fixture-noon")
    item(conn, 2)  # The same old timestamp, discovered after the frozen digest.
    calls = []

    def fake_mail(argv):
        calls.append(argv)
        assert argv[argv.index("--message-ids") + 1] == "mail-002"
        return CommandResult({"messages": [{
            "message_id": "mail-002", "internal_date": "1788231600000",
            "subject": "Late build", "body_plain_text": "fixture full body",
        }]}, "user", [])

    result = refresh_missing_bodies(conn, scheduled_at="2026-09-01T18:00:00+08:00", runner=fake_mail)
    assert result == {"selected": 1, "refreshed": 1} and len(calls) == 1
    event = conn.execute("SELECT payload_json FROM inbound_events WHERE external_id='mail-002:received'").fetchone()
    assert json.loads(event[0])["body_plain_text"] == "fixture full body"
    assert query_summary(conn, digest_id=noon["summary_id"])["message_count"] == 1


@pytest.mark.parametrize("body", ["x" * 12000, "\n\t\"\\" * 3000])
def test_complete_serialized_envelope_reserves_membership_before_body_budget(conn, body):
    for index in range(40):
        item(conn, index)
    conn.execute("UPDATE inbound_events SET payload_json=json_set(payload_json,'$.body_plain_text',?)", (body,))
    calls = []

    def summarizer(value):
        calls.append(value)
        assert len(canonical_json(value)) <= 180000
        assert len(value["category_membership"]) == len(value["messages"]) == 40
        assert all(row["body_plain_text"] for row in value["messages"])
        return {"overview": "构建", "categories": [{"category": "build_ci", "count": 40, "summary": "构建"}], "important": []}

    prepare_summary(conn, scheduled_at="2026-09-01T12:00:00+08:00", destination="telegram:owner-chat",
                    slot="mail_noon", summarizer=summarizer)
    assert len(calls) == 1


def test_two_summary_slots_cannot_reserve_same_members_during_model_call(conn):
    item(conn, 1)

    def racing_summarizer(value):
        # Interleaving is the same as a second connection committing a different
        # slot while the first model call is out of its read snapshot.
        prepare_summary(conn, scheduled_at="2026-09-01T18:00:00+08:00", destination="telegram:owner-chat",
                        slot="mail_evening", summarizer=lambda _: {
                            "overview": "Winner", "categories": [{"category": "build_ci", "count": 1, "summary": "Winner"}], "important": []})
        return {"overview": "Stale", "categories": [{"category": "build_ci", "count": 1, "summary": "Stale"}], "important": []}

    with pytest.raises(MailError, match="membership changed"):
        prepare_summary(conn, scheduled_at="2026-09-01T12:00:00+08:00", destination="telegram:owner-chat",
                        slot="mail_noon", summarizer=racing_summarizer)
    assert conn.execute("SELECT count(*) FROM mail_summary_membership").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM outbox WHERE action_type='mail_summary'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM mail_digest_runs").fetchone()[0] == 1


def test_summary_show_cli_never_initializes_or_migrates_database(config, capsys):
    from k3_support.cli import main

    config.path.write_text(yaml.safe_dump(config.raw), encoding="utf-8")
    assert not config.database_path.exists()
    assert main(["--config", str(config.path), "mail-summary-show", "missing"]) == 2
    assert not config.database_path.exists()
    assert "existing workflow database" in capsys.readouterr().err


def test_legacy_exclusion_uses_exact_epoch_milliseconds_and_only_existing_ids(tmp_path, config, monkeypatch):
    from k3_support import db

    legacy = db.connect(tmp_path / "legacy-mail.db")
    files = db.migration_files()
    with monkeypatch.context() as patch:
        patch.setattr(db, "migration_files", lambda: [entry for entry in files if entry[0] <= 26])
        db.migrate(legacy)
    for message_id, timestamp in (("boundary", 1788235200000), ("later", 1788235200001)):
        upsert_mail_item(legacy, {"message_id": message_id, "subject": "old", "internal_date": str(timestamp)})
    # 09:45 Nepal is exactly 04:00 UTC, avoiding a float Julian-day conversion.
    outbox_id = "legacy-summary"
    legacy.execute("""INSERT INTO outbox(outbox_id,channel,action_type,destination,
                   payload_json,idempotency_key,created_at,updated_at)
                   VALUES(?,'telegram','mail_summary','telegram:fixture',
                   '{"text":"legacy"}',?,'2026-09-01','2026-09-01')""",
                   (outbox_id, outbox_id))
    legacy.execute("""INSERT INTO summary_runs(summary_id,summary_type,watermark_key,range_end,
                    item_count,content_digest,state,created_at,outbox_id)
                    VALUES('legacy','mail_noon','mail_summary_delivered','2026-09-01T09:45:00+05:45',
                    1,'digest','delivered','2026-09-01T04:00:00+00:00',?)""", (outbox_id,))
    db.migrate(legacy)
    assert [row[0] for row in legacy.execute("SELECT message_id FROM mail_summary_legacy_exclusions")] == ["boundary"]
    upsert_mail_item(legacy, {"message_id": "late-old-timestamp", "subject": "late", "internal_date": "1788235199999"})
    assert not legacy.execute("SELECT 1 FROM mail_summary_legacy_exclusions WHERE message_id='late-old-timestamp'").fetchone()
    legacy.close()


def test_correction_digest_detects_same_time_same_category_aba(conn):
    message_id, _, _ = item(conn, 1)
    original_time = "2026-09-01T03:00:00+00:00"
    original_digest = classification_review_digest(conn, message_id)
    correct_category(conn, message_id=message_id, category="company", actor_id="operator", reason="first",
                     expected_updated_at=original_time, external_id="aba-one")
    current_time = conn.execute("SELECT updated_at FROM mail_catalog_items WHERE message_id=?", (message_id,)).fetchone()[0]
    correct_category(conn, message_id=message_id, category="build_ci", actor_id="operator", reason="revert",
                     expected_updated_at=current_time, external_id="aba-two")
    conn.execute("UPDATE mail_catalog_items SET updated_at=? WHERE message_id=?", (original_time, message_id))
    with pytest.raises(MailSnapshotError, match="review changed"):
        correct_category(conn, message_id=message_id, category="upstream", actor_id="operator", reason="old preview",
                         expected_updated_at=original_time, expected_current_digest=original_digest, external_id="aba-old-preview")
