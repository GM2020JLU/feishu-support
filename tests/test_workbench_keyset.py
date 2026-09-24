from __future__ import annotations

import base64
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from k3_support.store import create_case


def make_case(conn, title):
    return create_case(
        conn, title=title, case_type="bug", severity="P2", confidence=0.8
    )[0]


def test_registered_identity_survives_state_changes_and_cannot_be_reused(conn):
    case_id = make_case(conn, "first")
    row = conn.execute(
        "SELECT * FROM workbench_item_keys WHERE entity_kind='case' AND target_key=?",
        (case_id,),
    ).fetchone()
    assert row is not None
    conn.execute(
        "UPDATE cases SET severity='P0',state='takeover' WHERE case_id=?", (case_id,)
    )
    assert (
        conn.execute(
            "SELECT item_seq FROM workbench_item_keys WHERE target_key=?", (case_id,)
        ).fetchone()[0]
        == row["item_seq"]
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE workbench_item_keys SET target_key='other' WHERE item_seq=?",
            (row["item_seq"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE cases SET case_id='K3-other' WHERE case_id=?", (case_id,))


def test_keyset_survives_unrelated_changes_and_addresses_the_original_item(
    conn, config
):
    from k3_support.workbench import workbench_page
    from k3_support.workbench_navigation import route

    cases = [make_case(conn, f"first-{index}") for index in range(110)]
    first = workbench_page(conn, config, limit=4)
    clicked = next(
        button["callback_data"]
        for button in first["preview"]["buttons"]
        if button["callback_data"].startswith("wi2:")
    )
    next_cursor = first["snapshot"]["next_cursor"]
    conn.execute(
        "UPDATE cases SET severity='P0',state='takeover' WHERE case_id=?", (cases[0],)
    )
    make_case(conn, "new after opening")
    second = workbench_page(conn, config, cursor=next_cursor, limit=4)
    assert [item["case_id"] for item in second["snapshot"]["items"]] == cases[4:8]
    detail = route(conn, config, clicked)
    assert detail["case_id"] == cases[0]
    assert any(
        button["callback_data"] == first["snapshot"]["cursor"]
        for button in detail["preview"]["buttons"]
    )


def test_navigation_namespace_rotation_invalidates_old_cards(conn, config):
    from k3_support.workbench import workbench_page
    from k3_support.workbench_navigation import rotate_namespace, route

    make_case(conn, "first")
    first = workbench_page(conn, config)
    rotate_namespace(conn)
    with pytest.raises(ValueError, match="refresh"):
        route(conn, config, first["snapshot"]["cursor"])


def seed_sources(conn):
    from k3_support.approvals import request_approval
    from k3_support.knowledge import create_candidate

    now = datetime.now(UTC)
    case_id = make_case(conn, "all entity types")
    request_approval(
        conn,
        approval_type="board1_lease",
        case_id=case_id,
        action={"session": "fixture"},
        expires_at=(now + timedelta(days=1)).isoformat(),
    )
    create_candidate(
        conn,
        title="fixture knowledge",
        questions=["test"],
        answer_markdown="Only a fixture",
        project="K3",
        module="EC",
        software_version=None,
        disclosure_class="internal",
        confidence=0.9,
        source_authority=0.9,
        canonical_case_id=None,
        source_digest="fixture",
    )
    stamp = now.isoformat()
    outbox_id = "outbox-fixture"
    conn.execute(
        """INSERT INTO outbox(outbox_id,channel,action_type,destination,payload_json,idempotency_key,created_at,updated_at)
        VALUES(?,'telegram','notify','owner','{"text":"never sent"}','nav-fixture',?,?)""",
        (outbox_id, stamp, stamp),
    )
    conn.execute(
        """INSERT INTO jobs(job_id,job_type,state,input_digest,available_at,created_at,updated_at)
        VALUES('job-fixture','retrieve','queued','input',?,?,?)""",
        (stamp, stamp, stamp),
    )
    conn.execute(
        "INSERT INTO mail_items(message_id,received_at,updated_at) VALUES('mail:b',?,?)",
        (stamp, stamp),
    )
    conn.execute(
        """INSERT INTO mail_digest_runs(digest_id,summary_type,watermark_key,range_end,item_count,
        ai_summary_json,content_digest,telegram_destination,state,created_at)
        VALUES('digest:a','mail_noon','fixture',?,1,'{}','digest','owner','linking',?)""",
        (stamp, stamp),
    )
    conn.execute(
        """INSERT INTO mail_digest_links(digest_id,message_id,ordinal,share_outbox_id,state,created_at,updated_at)
        VALUES('digest:a','mail:b',0,?,'pending',?,?)""",
        (outbox_id, stamp, stamp),
    )
    return case_id


def test_all_six_entities_register_before_they_enter_a_queue(conn, config):
    from k3_support.workbench import workbench_page

    seed_sources(conn)
    keys = list(conn.execute("SELECT * FROM workbench_item_keys ORDER BY item_seq"))
    assert {row["entity_kind"] for row in keys} == {
        "case",
        "approval",
        "knowledge",
        "outbox",
        "job",
        "mail",
    }
    assert len(keys) == 6
    assert (
        next(row["target_key"] for row in keys if row["entity_kind"] == "mail")
        == '["digest:a","mail:b"]'
    )
    # Entities already existed at open time but were not queue members yet.
    first = workbench_page(conn, config)
    conn.execute("UPDATE outbox SET state='permanent_failure'")
    conn.execute("UPDATE jobs SET state='failed'")
    conn.execute("UPDATE mail_digest_links SET state='failed'")
    current = workbench_page(
        conn, config, cursor=first["snapshot"]["cursor"], limit=50, render_buttons=False
    )
    assert current["snapshot"]["total_items"] == 6
    assert list(
        map(tuple, conn.execute("SELECT * FROM workbench_item_keys ORDER BY item_seq"))
    ) == list(map(tuple, keys))


@pytest.mark.parametrize(
    "table,column",
    [
        ("cases", "case_id"),
        ("approvals", "approval_id"),
        ("knowledge_entries", "knowledge_id"),
        ("outbox", "outbox_id"),
        ("jobs", "job_id"),
        ("mail_digest_links", "digest_id"),
        ("mail_digest_links", "message_id"),
    ],
)
def test_source_primary_keys_are_immutable(conn, table, column):
    seed_sources(conn)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(f"UPDATE {table} SET {column}='different'")


def test_source_delete_is_a_tombstone_and_reinsert_cannot_retarget_old_buttons(
    conn, config
):
    from k3_support.workbench import workbench_page
    from k3_support.workbench_navigation import route

    seed_sources(conn)
    first = workbench_page(conn, config, view="knowledge")
    callback = next(
        button["callback_data"]
        for button in first["preview"]["buttons"]
        if button["callback_data"].startswith("wi2:")
    )
    row = dict(conn.execute("SELECT * FROM knowledge_entries").fetchone())
    conn.execute("DELETE FROM knowledge_entries")
    assert "该事项已移除" in route(conn, config, callback)["preview"]["text"]
    with pytest.raises(sqlite3.IntegrityError, match="cannot be reused"):
        conn.execute(
            "INSERT INTO knowledge_entries("
            + ",".join(row)
            + ") VALUES("
            + ",".join("?" for _ in row)
            + ")",
            tuple(row.values()),
        )
    assert (
        conn.execute(
            "SELECT count(*) FROM workbench_item_keys WHERE entity_kind='knowledge'"
        ).fetchone()[0]
        == 1
    )


def test_idempotent_source_insert_does_not_allocate_new_identity(conn):
    seed_sources(conn)
    row = dict(conn.execute("SELECT * FROM jobs").fetchone())
    before = list(map(tuple, conn.execute("SELECT * FROM workbench_item_keys")))
    conn.execute(
        "INSERT OR IGNORE INTO jobs("
        + ",".join(row)
        + ") VALUES("
        + ",".join("?" for _ in row)
        + ")",
        tuple(row.values()),
    )
    assert list(map(tuple, conn.execute("SELECT * FROM workbench_item_keys"))) == before


def test_legacy_migration_backfills_by_utc_then_kind_and_never_by_priority(monkeypatch):
    from k3_support import db

    legacy = sqlite3.connect(":memory:", isolation_level=None)
    legacy.row_factory = sqlite3.Row
    migrations = db.migration_files()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                db,
                "migration_files",
                lambda: [entry for entry in migrations if entry[0] < 30],
            )
            db.migrate(legacy)
        seed_sources(legacy)
        for table, stamp in (
            ("knowledge_entries", "invalid"),
            ("cases", "2026-09-01T09:00:00+08:00"),
            ("jobs", "2026-09-01T01:00:00Z"),
            ("outbox", "2026-09-01T02:00:00Z"),
            ("approvals", "2026-09-01T03:00:00Z"),
            ("mail_digest_links", "2026-09-01T04:00:00Z"),
        ):
            legacy.execute(f"UPDATE {table} SET created_at=?", (stamp,))
        assert db.migrate(legacy) == [
            entry[0] for entry in migrations if entry[0] >= 30
        ]
        assert [
            row[0]
            for row in legacy.execute(
                "SELECT entity_kind FROM workbench_item_keys ORDER BY item_seq"
            )
        ] == ["knowledge", "case", "job", "outbox", "approval", "mail"]
        first = legacy.execute(
            "SELECT instance_id FROM workbench_navigation_namespace"
        ).fetchone()[0]
        assert isinstance(first, bytes) and len(first) == 16
        assert (
            legacy.execute(
                "SELECT legacy_invalid_timestamps FROM workbench_navigation_namespace"
            ).fetchone()[0]
            == 1
        )
        assert db.migrate(legacy) == []
        assert (
            legacy.execute(
                "SELECT instance_id FROM workbench_navigation_namespace"
            ).fetchone()[0]
            == first
        )
    finally:
        legacy.close()


def test_original_anchor_can_leave_filter_without_breaking_next_or_previous(
    conn, config
):
    from k3_support.workbench import workbench_page
    from k3_support.workbench_navigation import route

    cases = [make_case(conn, f"case-{i}") for i in range(12)]
    first = workbench_page(conn, config, view="ai")
    callback = next(
        button["callback_data"]
        for button in first["preview"]["buttons"]
        if button["callback_data"].startswith("wi2:")
    )
    conn.execute(
        "UPDATE cases SET state='takeover' WHERE case_id IN (?,?)", (cases[0], cases[3])
    )
    second = workbench_page(conn, config, cursor=first["snapshot"]["next_cursor"])
    assert [item["case_id"] for item in second["snapshot"]["items"]] == cases[4:8]
    previous = workbench_page(
        conn, config, cursor=second["snapshot"]["previous_cursor"]
    )
    assert [item["case_id"] for item in previous["snapshot"]["items"]] == cases[1:3]
    detail = route(conn, config, callback)
    assert detail["case_id"] == cases[0]
    assert "已不在原筛选" in detail["preview"]["text"]
    assert detail["origin_cursor"] == first["snapshot"]["cursor"]


def test_readonly_cursor_paths_do_not_migrate_backfill_or_mutate(conn, config):
    from k3_support.workbench import workbench_page
    from k3_support.workbench_navigation import route

    make_case(conn, "only read")
    conn.commit()
    readonly = sqlite3.connect(f"file:{config.database_path}?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    try:
        before = list(readonly.iterdump())
        first = workbench_page(readonly, config)
        callback = next(
            button["callback_data"]
            for button in first["preview"]["buttons"]
            if button["callback_data"].startswith("wi2:")
        )
        detail = route(readonly, config, callback)
        route(readonly, config, detail["origin_cursor"])
        assert readonly.total_changes == 0 and list(readonly.iterdump()) == before
    finally:
        readonly.close()


def test_binary_callback_lengths_reach_protocol_maximum_without_overflow(conn):
    from k3_support.workbench_navigation import MAX_SEQ, Navigation, encode, namespace

    nav = Navigation(namespace(conn), "closed", MAX_SEQ, MAX_SEQ, True)
    assert len(encode(nav).encode()) == 43
    assert len(encode(nav, item_seq=MAX_SEQ).encode()) == 51
    assert (
        len(encode(nav, item_seq=MAX_SEQ, page=65535, content_digest="f" * 16).encode())
        == 64
    )
    # Existing action token stays independently bound to current authority.
    assert len(("wka2:r:" + "K3-" + "x" * 28 + ":" + "x" * 22).encode()) == 61


def test_sequence_exhaustion_aborts_source_creation_instead_of_omitting_its_index(conn):
    from k3_support.workbench_navigation import MAX_SEQ

    seed_sources(conn)
    conn.execute(
        "UPDATE sqlite_sequence SET seq=? WHERE name='workbench_item_keys'", (MAX_SEQ,)
    )
    before = conn.execute("SELECT count(*) FROM cases").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="exhausted"):
        make_case(conn, "must not become an unindexed item")
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == before


@pytest.mark.parametrize(
    "kind",
    [
        "padding",
        "bad_alphabet",
        "reserved",
        "view",
        "anchor",
        "future_upper",
        "short",
        "target_zero",
        "page_zero",
        "legacy",
        "noncanonical",
    ],
)
def test_malformed_cursors_fail_closed_without_writes(conn, config, kind):
    from k3_support.workbench_navigation import encode, open_navigation, route

    make_case(conn, "fixture")
    nav = open_navigation(conn)
    callback = encode(nav, item_seq=1, page=1, content_digest="1" * 16)
    raw = bytearray(
        base64.urlsafe_b64decode(callback[4:] + "=" * (-len(callback[4:]) % 4))
    )
    if kind == "padding":
        callback += "="
    elif kind == "bad_alphabet":
        callback = callback[:-1] + "/"
    elif kind == "legacy":
        callback = "wki:all:1:1:0123456789abcdef"
    elif kind == "noncanonical":
        base = encode(nav)
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        callback = base[:-1] + alphabet[alphabet.index(base[-1]) | 1]
    else:
        if kind == "reserved":
            raw[16] = 32
        if kind == "view":
            raw[16] = 15
        if kind == "anchor":
            raw[23:29] = (2).to_bytes(6, "big")
        if kind == "future_upper":
            raw[17:23] = (2).to_bytes(6, "big")
        if kind == "target_zero":
            raw[29:35] = b"\0" * 6
        if kind == "page_zero":
            raw[35:37] = b"\0" * 2
        if kind == "short":
            raw.pop()
        callback = "wd2:" + base64.urlsafe_b64encode(raw).decode().rstrip("=")
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        route(conn, config, callback)
    assert list(conn.iterdump()) == before


def test_opening_bound_excludes_new_entities_but_refresh_includes_them(conn, config):
    from k3_support.workbench import workbench_page
    from k3_support.workbench_navigation import decode, encode, route

    cases = [make_case(conn, f"old-{index}") for index in range(110)]
    first = workbench_page(conn, config)
    new_case = make_case(conn, "after open")
    nav = decode(conn, first["snapshot"]["cursor"])[0]
    cursor, found = first["snapshot"]["cursor"], []
    while cursor:
        page = workbench_page(conn, config, cursor=cursor)
        found.extend(item["case_id"] for item in page["snapshot"]["items"])
        cursor = page["snapshot"]["next_cursor"]
    assert found == cases and new_case not in found
    assert route(conn, config, "wb2:open:a")["snapshot"]["total_items"] == 111
    # Changing the filter retains the opening bound.
    assert (
        workbench_page(conn, config, cursor=encode(replace(nav, view="ai")))[
            "snapshot"
        ]["total_items"]
        == 110
    )


def test_action_with_an_unrelated_earlier_return_bound_fails_before_mutation(
    conn, config
):
    from k3_support.case_actions import action_binding
    from k3_support.control import ControlMessage, execute_control
    from k3_support.workbench import workbench_page

    empty_cursor = workbench_page(conn, config)["snapshot"]["cursor"]
    case_id = make_case(conn, "not in the old opening range")
    token = action_binding(conn, case_id=case_id, action="resolve")["token"]
    before = list(conn.iterdump())
    with pytest.raises(ValueError, match="range"):
        execute_control(
            conn,
            config,
            ControlMessage(
                "owner-user",
                "owner-chat",
                "bad-origin",
                f"case-action resolve {case_id} {token} return {empty_cursor}",
            ),
        )
    assert list(conn.iterdump()) == before


def test_long_external_list_fields_are_escaped_bounded_and_explicitly_partial(
    conn, config
):
    from k3_support.workbench import workbench_page
    from k3_support.workbench_navigation import route

    for index in range(4):
        case_id = make_case(conn, str(index) + "<&😀>" * 1000)
        conn.execute(
            "UPDATE cases SET next_action=? WHERE case_id=?",
            ("保留温控<&😀>" * 1000, case_id),
        )
    panel = workbench_page(conn, config)
    text = panel["preview"]["text"]
    assert len(text) < 4096 and len(text.encode("utf-16-le")) // 2 < 4096
    assert "&lt;" in text and "<😀>" not in text
    assert "简要预览" in text and "完整记录与约束" in text
    callback = next(
        button["callback_data"]
        for button in panel["preview"]["buttons"]
        if button["callback_data"].startswith("wi2:")
    )
    detail = route(conn, config, callback)
    assert detail["preview"]["page_count"] > 1
    assert all(
        len(button["callback_data"].encode()) <= 64
        for button in detail["preview"]["buttons"]
    )


def test_cli_report_presentation_explicitly_omits_telegram_buttons(conn, config):
    from k3_support.workbench import workbench_page

    for index in range(12):
        make_case(conn, str(index))
    with pytest.raises(ValueError, match="Telegram"):
        workbench_page(conn, config, limit=12)
    report = workbench_page(conn, config, limit=12, render_buttons=False)
    assert len(report["snapshot"]["items"]) == 12
    assert report["preview"]["buttons"] == []
    assert report["preview"]["display_target"] == "report"


def test_case_authority_change_only_invalidates_action_not_list_navigation(
    conn, config
):
    from k3_support.control import ControlMessage, execute_control
    from k3_support.workbench import workbench_page
    from k3_support.workbench_navigation import route

    case_id = make_case(conn, "first")
    first = workbench_page(conn, config)
    callback = next(
        button["callback_data"]
        for button in first["preview"]["buttons"]
        if button["callback_data"].startswith("wi2:")
    )
    detail = route(conn, config, callback)
    action = next(
        button["callback_data"]
        for button in detail["preview"]["buttons"]
        if button["callback_data"].startswith("wka2:r:")
    )
    conn.execute(
        "UPDATE cases SET version=version+1,state='takeover' WHERE case_id=?",
        (case_id,),
    )
    assert route(conn, config, callback)["case_id"] == case_id
    assert (
        route(conn, config, first["snapshot"]["cursor"])["snapshot"]["items"][0][
            "case_id"
        ]
        == case_id
    )
    before = list(conn.iterdump())
    with pytest.raises(ValueError, match="stale"):
        execute_control(
            conn,
            config,
            ControlMessage(
                "owner-user",
                "owner-chat",
                "stale-action",
                f"case-action resolve {case_id} {action.rsplit(':', 1)[1]} return {first['snapshot']['cursor']}",
            ),
        )
    assert list(conn.iterdump()) == before
