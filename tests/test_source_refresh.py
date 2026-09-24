from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from k3_support.db import connect
from k3_support.knowledge import (
    attach_registered_source,
    create_candidate,
    register_source,
    review,
)
from k3_support.lark import CommandResult, LarkError
from k3_support.source_refresh import refresh_registered_sources


def linked_source(conn, *, checked_at):
    register_source(
        conn,
        source_type="feishu_doc",
        stable_external_id="wiki:fan",
        title="风扇指南",
        url="https://example.feishu.cn/wiki/fan",
        acl={"visibility": "internal"},
        source_version="1",
        content_digest=hashlib.sha256(b"old").hexdigest(),
        updated_at=checked_at,
    )
    knowledge_id = create_candidate(
        conn,
        title="风扇指南路由",
        questions=["pico 风扇怎么调"],
        answer_markdown="文档链接",
        project="K3",
        module="EC",
        software_version=None,
        disclosure_class="internal",
        confidence=0.95,
        source_authority=0.95,
        canonical_case_id=None,
        source_digest="refresh-test",
    )
    attach_registered_source(
        conn,
        knowledge_id=knowledge_id,
        source_type="feishu_doc",
        stable_external_id="wiki:fan",
        claim="contains the reviewed procedure",
    )
    review(conn, knowledge_id=knowledge_id, reviewer_id="owner", decision="approved")
    conn.execute(
        "UPDATE source_registry SET last_checked_at=?",
        (checked_at,),
    )
    return knowledge_id


def test_source_refresh_marks_attached_knowledge_stale_on_revision_drift(conn):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    knowledge_id = linked_source(conn, checked_at=(now - timedelta(days=2)).isoformat())

    result = refresh_registered_sources(
        conn,
        now=now,
        runner=lambda argv: CommandResult(
            {"document": {"revision_id": "2", "content": "new content"}},
            "user",
            [],
        ),
    )

    assert result["counts"]["changed"] == 1
    assert "new content" not in str(result)
    assert (
        conn.execute(
            "SELECT status FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)
        ).fetchone()[0]
        == "stale"
    )
    assert (
        conn.execute("SELECT source_version FROM source_registry").fetchone()[0] == "2"
    )


def test_failed_source_refresh_preserves_last_good_comparison_point(conn):
    now = datetime(2026, 9, 3, tzinfo=UTC)
    old = (now - timedelta(days=2)).isoformat()
    linked_source(conn, checked_at=old)

    result = refresh_registered_sources(
        conn,
        now=now,
        runner=lambda argv: (_ for _ in ()).throw(
            LarkError("unavailable", error_type="transport")
        ),
    )

    assert result["counts"]["failed"] == 1
    assert tuple(
        conn.execute(
            "SELECT source_version,last_checked_at FROM source_registry"
        ).fetchone()
    ) == ("1", old)


def test_source_refresh_does_not_requeue_unsupported_sources_forever(conn):
    register_source(
        conn,
        source_type="feishu_wiki_sheet",
        stable_external_id="wiki:sheet",
        title="Reference sheet",
        url="https://example.feishu.cn/wiki/sheet",
        acl={"visibility": "internal"},
        source_version=None,
        content_digest=None,
        updated_at=None,
    )

    result = refresh_registered_sources(
        conn,
        runner=lambda _argv: (_ for _ in ()).throw(
            AssertionError("unsupported source must not be fetched")
        ),
    )

    assert result["selected"] == 0
    assert result["still_overdue"] == 0
    assert result["ignored_unsupported"] == 1


def _success(_argv):
    return CommandResult(
        {"document": {"revision_id": "2", "content": "new"}}, "user", []
    )


def test_failed_source_yields_to_other_sources_and_recovers_after_backoff(
    conn, tmp_path
):
    now = datetime(2026, 9, 6, tzinfo=UTC)
    old = (now - timedelta(days=3)).isoformat()
    linked_source(conn, checked_at=old)
    other = register_source(
        conn,
        source_type="feishu_doc",
        stable_external_id="wiki:other",
        title="Other",
        url="https://example.feishu.cn/wiki/other",
        acl={"visibility": "internal"},
        source_version="1",
        content_digest=None,
        updated_at=None,
    )
    conn.execute(
        "UPDATE source_registry SET last_checked_at=? WHERE source_id=?",
        ((now - timedelta(days=2)).isoformat(), other["source_id"]),
    )

    first = refresh_registered_sources(
        conn,
        now=now,
        limit=1,
        runner=lambda _: (_ for _ in ()).throw(subprocess.TimeoutExpired("fetch", 30)),
    )
    assert first["schedule"]["retry_deferred"] == 1
    assert first["still_overdue"] == 2
    # A fresh connection proves the retry schedule survives process restart.
    restarted = connect(tmp_path / "support.db")
    try:
        second = refresh_registered_sources(
            restarted, now=now, limit=1, runner=_success
        )
        assert second["results"][0]["source_id"] == other["source_id"]
        assert second["still_overdue"] == 1
        recovered = refresh_registered_sources(
            restarted,
            now=now + timedelta(minutes=5),
            runner=_success,
        )
        assert recovered["counts"]["changed"] == 1
        assert recovered["schedule"]["failed_sources"] == 0
        assert recovered["still_overdue"] == 0
    finally:
        restarted.close()


def test_refresh_lease_excludes_other_worker_and_recovers_from_crash(conn, tmp_path):
    now = datetime(2026, 9, 6, tzinfo=UTC)
    linked_source(conn, checked_at=(now - timedelta(days=2)).isoformat())
    other = connect(tmp_path / "support.db")

    def crashing_runner(_):
        concurrent = refresh_registered_sources(other, now=now, runner=_success)
        assert concurrent["selected"] == 0
        assert concurrent["schedule"]["in_flight"] == 1
        raise SystemExit("simulated worker crash")

    try:
        with pytest.raises(SystemExit):
            refresh_registered_sources(conn, now=now, runner=crashing_runner)
        assert (
            refresh_registered_sources(other, now=now, runner=_success)["selected"] == 0
        )
        result = refresh_registered_sources(
            other, now=now + timedelta(minutes=3), runner=_success
        )
        assert result["counts"]["changed"] == 1
        assert result["schedule"]["in_flight"] == 0
    finally:
        other.close()


def test_late_worker_cannot_overwrite_successor_snapshot(conn, tmp_path):
    now = datetime(2026, 9, 6, tzinfo=UTC)
    linked_source(conn, checked_at=(now - timedelta(days=2)).isoformat())
    other = connect(tmp_path / "support.db")

    def late_runner(_):
        fresh = refresh_registered_sources(
            other,
            now=now + timedelta(minutes=3),
            runner=_success,
        )
        assert fresh["counts"]["changed"] == 1
        return CommandResult(
            {"document": {"revision_id": "1", "content": "late old"}}, "user", []
        )

    try:
        result = refresh_registered_sources(conn, now=now, limit=1, runner=late_runner)
        assert result["counts"]["superseded"] == 1
        assert (
            conn.execute("SELECT source_version FROM source_registry").fetchone()[0]
            == "2"
        )
    finally:
        other.close()


def test_source_change_during_fetch_preserves_new_acl_and_coordinate(conn):
    now = datetime(2026, 9, 6, tzinfo=UTC)
    linked_source(conn, checked_at=(now - timedelta(days=2)).isoformat())

    def runner(_):
        register_source(
            conn,
            source_type="feishu_doc",
            stable_external_id="wiki:fan",
            title="New guide",
            url="https://example.feishu.cn/wiki/replacement",
            acl={"visibility": "private"},
            source_version="3",
            content_digest=None,
            updated_at=None,
            checked_at=now.isoformat(),
        )
        return _success([])

    result = refresh_registered_sources(conn, now=now, runner=runner)
    assert result["counts"]["superseded"] == 1
    source = conn.execute(
        "SELECT source_version,acl_json FROM source_registry"
    ).fetchone()
    assert source["source_version"] == "3"
    assert "private" in source["acl_json"]


def test_snapshot_and_retry_state_roll_back_together_on_db_failure(conn):
    now = datetime(2026, 9, 6, tzinfo=UTC)
    old = (now - timedelta(days=2)).isoformat()
    knowledge_id = linked_source(conn, checked_at=old)
    conn.execute("""CREATE TRIGGER fail_refresh BEFORE UPDATE ON source_refresh_state
                    WHEN NEW.last_state='changed'
                    BEGIN SELECT RAISE(ABORT,'injected persistence failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected persistence"):
        refresh_registered_sources(conn, now=now, runner=_success)
    assert tuple(
        conn.execute(
            "SELECT source_version,last_checked_at FROM source_registry"
        ).fetchone()
    ) == ("1", old)
    assert (
        conn.execute(
            "SELECT status FROM knowledge_entries WHERE knowledge_id=?", (knowledge_id,)
        ).fetchone()[0]
        == "approved"
    )
    assert (
        conn.execute("SELECT last_state FROM source_refresh_state").fetchone()[0]
        == "refreshing"
    )
    assert conn.in_transaction is False


def test_success_respects_new_freshness_policy_and_failures_back_off(conn):
    now = datetime(2026, 9, 6, tzinfo=UTC)
    linked_source(conn, checked_at=(now - timedelta(days=2)).isoformat())
    refresh_registered_sources(conn, now=now, runner=_success)
    fresh = refresh_registered_sources(
        conn, now=now + timedelta(hours=2), max_age_hours=1, runner=_success
    )
    assert fresh["counts"]["unchanged"] == 1
    assert (
        conn.execute("SELECT last_checked_at FROM source_registry").fetchone()[0]
        == (now + timedelta(hours=2)).isoformat()
    )
    failed_at = now + timedelta(days=2)

    def fail(_):
        raise LarkError("transport details must not be stored", error_type="transport")

    refresh_registered_sources(conn, now=failed_at, runner=fail)
    result = refresh_registered_sources(
        conn, now=failed_at + timedelta(minutes=5), runner=fail
    )
    failure = result["schedule"]["recent_failures"][0]
    assert failure["failure_count"] == 2
    assert failure["next_attempt_at"] == (failed_at + timedelta(minutes=15)).isoformat()
    assert "transport details" not in str(result)
