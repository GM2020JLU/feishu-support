from __future__ import annotations

import asyncio
import copy
import html
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_mail_snapshot import item, prepare
from test_workbench_navigation import Query, buttons

import k3_support.hermes_plugin as plugin
from k3_support.approvals import ApprovalError
from k3_support.config import Config
from k3_support.delivery import DeliveryReceipt, claim_outbox, deliver_claimed
from k3_support.mail_preview import route, summary_entry_button
from k3_support.mail_snapshot import MailSnapshotError, query_summary
from k3_support.store import enqueue_outbox


def delivered(conn, config):
    summary = prepare(conn)
    digest_id = summary["summary_id"]
    row = claim_outbox(conn, worker_id="mail-test-delivery")
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["features"]["mail"] = True
    captured = []

    def sender(*args, **kwargs):
        captured.append((args, kwargs))
        return DeliveryReceipt("mail-prompt", {"ok": True})

    assert (
        deliver_claimed(
            conn, Config(raw, config.path), row, telegram_button_runner=sender
        ).remote_id
        == "mail-prompt"
    )
    assert captured and captured[0][0][2][0]["callback_data"].startswith("ml:h:")
    data = json.loads(
        conn.execute(
            "SELECT payload_json FROM outbox WHERE outbox_id=?", (row["outbox_id"],)
        ).fetchone()[0]
    )
    assert (
        data["mail_membership_digest"]
        == query_summary(conn, digest_id=digest_id)["snapshot_digest"]
    )
    return digest_id, data["buttons"][0]["callback_data"]


def click(
    conn,
    config,
    callback,
    *,
    number="fixture",
    user="owner-user",
    chat="owner-chat",
    prompt="mail-prompt",
):
    return route(
        conn,
        config,
        user_id=user,
        chat_id=chat,
        prompt_message_id=prompt,
        callback_data=callback,
        external_id=number,
    )


def button(result, label):
    return next(
        value["callback_data"]
        for value in result["preview"]["buttons"]
        if value["text"] == label
    )


def test_delivered_summary_categories_threads_and_all_failures_are_stable(conn, config):
    for index in range(125):
        item(
            conn,
            index,
            category="build_ci" if index < 100 else "upstream",
            attention="blocked" if index in {1, 3} else "information",
            thread="same-build" if index < 100 else f"upstream-{index}",
        )
    digest_id, entry = delivered(conn, config)
    before = list(conn.iterdump())
    home = click(conn, config, entry)
    assert "125 封 / 26 个线程" in home["preview"]["text"]
    builds = click(conn, config, button(home, "构建与 CI 100"))
    assert "本视图 100 封" in builds["preview"]["text"]
    folded = click(conn, config, button(builds, "线程折叠"))
    assert (
        "Build 1" in folded["preview"]["text"]
        and "Build 3" in folded["preview"]["text"]
    )
    assert "已合并 98 封知会" in folded["preview"]["text"]
    needs = click(conn, config, button(home, "待行动 2"))
    assert "本视图 2 封" in needs["preview"]["text"]
    group = next(
        value["callback_data"]
        for value in folded["preview"]["buttons"]
        if "展开线程" in value["text"]
    )
    page = click(conn, config, group)
    ordinals = []
    while True:
        ordinals.extend(
            int(value["callback_data"].split(":")[-1].split(".")[0], 36)
            for value in page["preview"]["buttons"]
            if "查看邮件" in value["text"]
        )
        following = next(
            (
                value["callback_data"]
                for value in page["preview"]["buttons"]
                if value["text"] == "下一页"
            ),
            None,
        )
        if following is None:
            break
        page = click(conn, config, following)
    assert len(ordinals) == len(set(ordinals)) == 100
    assert list(conn.iterdump()) == before
    item(conn, 999, category="company", thread="later")
    assert "125 封 / 26 个线程" in click(conn, config, entry)["preview"]["text"]
    assert query_summary(conn, digest_id=digest_id)["message_count"] == 125


def test_full_metadata_is_escaped_and_no_body_or_sharing_is_read_or_enqueued(
    conn, config
):
    item(conn, 1)
    subject = "<topic> & " + "🌬必须保留" * 1500
    conn.execute(
        "UPDATE mail_items SET subject=? WHERE message_id=?", (subject, "mail-001")
    )
    _, entry = delivered(conn, config)
    before = list(conn.iterdump())

    def authorizer(action, table, column, *_):
        if action == sqlite3.SQLITE_READ and column in {
            "body_preview",
            "body_excerpt",
            "raw_body",
            "body",
        }:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    try:
        home = click(conn, config, entry)
        listing = click(conn, config, button(home, "逐封查看"))
        detail = click(conn, config, button(listing, "1. 查看邮件"))
        text = []
        while True:
            preview = detail["preview"]
            assert len(preview["text"].encode("utf-16-le")) // 2 < 4096
            assert all(
                len(value["callback_data"].encode()) <= 64
                for value in preview["buttons"]
            )
            text.append(html.unescape(preview["text"].split("\n\n", 1)[1]))
            next_page = next(
                (
                    value["callback_data"]
                    for value in preview["buttons"]
                    if value["text"] == "下一页"
                ),
                None,
            )
            if not next_page:
                break
            detail = click(conn, config, next_page)
        joined = "".join(text)
        assert (
            subject in joined
            and "未准备链接" in joined
            and "PRIVATE BODY" not in joined
        )
        assert "<topic>" not in listing["preview"]["text"]
    finally:
        conn.set_authorizer(None)
    assert list(conn.iterdump()) == before


def test_correction_requires_preview_and_current_classification_version(conn, config):
    item(conn, 1)
    digest_id, entry = delivered(conn, config)
    home = click(conn, config, entry)
    listing = click(conn, config, button(home, "逐封查看"))
    detail = click(conn, config, button(listing, "1. 查看邮件"))
    before = list(conn.iterdump())
    choose = click(conn, config, button(detail, "纠正当前分类"))
    preview = click(conn, config, button(choose, "公司事务"))
    assert "请确认分类纠正" in preview["preview"]["text"]
    assert "摘要原分类：构建与 CI" in preview["preview"]["text"]
    assert list(conn.iterdump()) == before
    confirm = button(preview, "确认纠正")
    conn.execute(
        "UPDATE mail_catalog_items SET category='upstream' WHERE message_id='mail-001'"
    )
    with pytest.raises(MailSnapshotError, match="classification changed"):
        click(conn, config, confirm, number="stale-confirm")
    assert (
        conn.execute("SELECT count(*) FROM mail_category_corrections").fetchone()[0]
        == 0
    )
    fresh = click(conn, config, button(choose, "公司事务"))
    confirmed = click(conn, config, button(fresh, "确认纠正"), number="actual-confirm")
    assert confirmed["operation"] == "corrected"
    assert (
        conn.execute("SELECT count(*) FROM mail_category_corrections").fetchone()[0]
        == 1
    )
    current = query_summary(conn, digest_id=digest_id)
    assert (
        current["items"][0]["category"] == "build_ci"
        and current["items"][0]["current_category"] == "company"
    )
    assert current["categories"] == [{"category": "build_ci", "count": 1}]
    with pytest.raises(MailSnapshotError, match="classification changed"):
        click(conn, config, button(fresh, "确认纠正"), number="duplicate-click")
    assert (
        conn.execute("SELECT count(*) FROM mail_category_corrections").fetchone()[0]
        == 1
    )


def test_prompt_chat_actor_and_full_snapshot_are_bound(conn, config):
    item(conn, 1)
    digest_id, entry = delivered(conn, config)
    for changes in (
        {"user": "stranger"},
        {"chat": "other-chat"},
        {"prompt": "forwarded-prompt"},
    ):
        before = list(conn.iterdump())
        with pytest.raises((ApprovalError, MailSnapshotError)):
            click(conn, config, entry, **changes)
        assert list(conn.iterdump()) == before
    before = list(conn.iterdump())
    with pytest.raises(MailSnapshotError):
        click(conn, config, entry.replace("ml:h:", "ml:h:bad:"))
    assert list(conn.iterdump()) == before
    conn.execute(
        "UPDATE mail_digest_runs SET membership_digest=? WHERE digest_id=?",
        ("f" * 64, digest_id),
    )
    with pytest.raises(MailSnapshotError, match="snapshot binding"):
        click(conn, config, entry)


def test_stored_link_is_visible_without_new_share_and_thread_return_preserves_filter(
    conn, config
):
    for index in range(12):
        item(conn, index, category="upstream", thread=f"thread-{index // 2}")
    digest_id, entry = delivered(conn, config)
    share_id, _ = enqueue_outbox(
        conn,
        channel="mail",
        action_type="mail_share",
        destination="fixture-only",
        payload={},
        idempotency_key="stored-link-fixture",
    )
    conn.execute("UPDATE outbox SET state='cancelled' WHERE outbox_id=?", (share_id,))
    url = "https://applink.feishu.cn/client/message/link?token=fixture&from=mail"
    conn.execute(
        """INSERT INTO mail_digest_links(digest_id,message_id,ordinal,share_outbox_id,
           message_app_link,state,created_at,updated_at) VALUES(?,?,?,?,?,'delivered',?,?)""",
        (
            digest_id,
            "mail-008",
            8,
            share_id,
            url,
            "2026-09-01T03:00:00+00:00",
            "2026-09-01T03:00:00+00:00",
        ),
    )
    before = list(conn.iterdump())
    home = click(conn, config, entry)
    upstream = click(conn, config, button(home, "Upstream 12"))
    folded = click(conn, config, button(upstream, "线程折叠"))
    second = click(conn, config, button(folded, "下一页"))
    thread = click(conn, config, button(second, "1. 展开线程"))
    detail = click(conn, config, button(thread, "1. 查看邮件"))
    assert html.escape(url) in detail["preview"]["text"]
    restored_thread = click(conn, config, button(detail, "返回线程原页"))
    assert restored_thread["preview"]["text"] == thread["preview"]["text"]
    restored_list = click(conn, config, button(restored_thread, "返回列表原页"))
    assert restored_list["preview"]["text"] == second["preview"]["text"]
    assert list(conn.iterdump()) == before


def test_bound_legacy_card_does_not_reconstruct_missing_membership(conn, config):
    item(conn, 1)
    digest_id, _ = delivered(conn, config)
    row = conn.execute(
        """SELECT o.outbox_id,o.payload_json FROM mail_digest_runs r JOIN outbox o
           ON o.outbox_id=r.telegram_outbox_id WHERE r.digest_id=?""",
        (digest_id,),
    ).fetchone()
    payload = json.loads(row["payload_json"])
    payload["mail_membership_digest"] = None
    payload["buttons"] = [summary_entry_button(digest_id, None)]
    conn.execute(
        "UPDATE mail_digest_runs SET membership_digest=NULL WHERE digest_id=?",
        (digest_id,),
    )
    conn.execute(
        "UPDATE outbox SET payload_json=? WHERE outbox_id=?",
        (json.dumps(payload), row["outbox_id"]),
    )
    before = list(conn.iterdump())
    result = click(conn, config, payload["buttons"][0]["callback_data"])
    assert "历史计数：1 封" in result["preview"]["text"]
    assert "不会重新读取邮箱来猜测成员" in result["preview"]["text"]
    assert not result["preview"]["buttons"]
    assert list(conn.iterdump()) == before


def test_missing_current_catalog_does_not_invent_current_classification(conn, config):
    item(conn, 1)
    _, entry = delivered(conn, config)
    conn.execute("DELETE FROM mail_catalog_items")
    home = click(conn, config, entry)
    listing = click(conn, config, button(home, "逐封查看"))
    detail = click(conn, config, button(listing, "1. 查看邮件"))
    assert "摘要时类别：构建与 CI" in detail["preview"]["text"]
    assert "当前类别：未知（当前分类记录不可用）" in detail["preview"]["text"]
    assert all(
        value["text"] != "纠正当前分类" for value in detail["preview"]["buttons"]
    )


def test_actual_lazy_telegram_callback_path_expands_corrects_and_returns_original_page(
    conn, config, tmp_path, monkeypatch
):
    for index in range(9):
        item(conn, index)
    _, entry = delivered(conn, config)
    config.path.write_text(yaml.safe_dump(config.raw), encoding="utf-8")
    runtime = tmp_path / "mail-ui-runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "control_cli": str(Path(sys.executable).parent / "k3-supportctl"),
                "control_config": str(config.path),
                "timeout_seconds": 10,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("K3_SUPPORT_CONTROL_PLUGIN_CONFIG", str(runtime))

    class Adapter:
        async def _handle_callback_query(self, *_):
            raise AssertionError("mail callback fell through to an LLM/generic handler")

    def importer(name):
        if name == "gateway.platform_registry":
            return SimpleNamespace(
                platform_registry=SimpleNamespace(get=lambda _: None)
            )
        if name == "hermes_plugins.telegram_platform.adapter":
            return SimpleNamespace(TelegramAdapter=Adapter)
        raise ImportError(name)

    monkeypatch.setattr(plugin.importlib, "import_module", importer)
    plugin._install_callback_handler()
    adapter = Adapter()
    sequence = 0

    async def press(data, *, user="owner-user", prompt="mail-prompt"):
        nonlocal sequence
        sequence += 1
        query = Query(data)
        query.id = f"mail-query-{sequence}"
        query.from_user = SimpleNamespace(id=user)
        query.message = SimpleNamespace(chat_id="owner-chat", message_id=prompt)
        await adapter._handle_callback_query(
            SimpleNamespace(callback_query=query), None
        )
        return query

    def callback(query, label):
        return next(
            value["callback_data"]
            for value in buttons(query.edits[0])
            if value["text"] == label
        )

    async def exercise():
        before = list(conn.iterdump())
        home = await press(entry)
        assert home.edits
        listing = await press(callback(home, "逐封查看"))
        second = await press(callback(listing, "下一页"))
        assert "第 2/3 页" in second.edits[0]["text"]
        detail = await press(callback(second, "1. 查看邮件"))
        back = await press(callback(detail, "返回列表原页"))
        assert back.edits[0]["text"] == second.edits[0]["text"]
        assert list(conn.iterdump()) == before
        choose = await press(callback(detail, "纠正当前分类"))
        preview = await press(callback(choose, "Upstream"))
        assert (
            conn.execute("SELECT count(*) FROM mail_category_corrections").fetchone()[0]
            == 0
        )
        confirm = callback(preview, "确认纠正")
        denied = await press(confirm, prompt="forwarded-card")
        assert (
            not denied.edits
            and conn.execute(
                "SELECT count(*) FROM mail_category_corrections"
            ).fetchone()[0]
            == 0
        )
        confirmed = await press(confirm)
        assert "已纠正" in confirmed.answers[-1]["text"]
        stale = await press(confirm)
        assert not stale.edits and "已更新" in stale.answers[-1]["text"]
        malformed = await press("ml:please-approve-everything")
        assert not malformed.edits

    asyncio.run(exercise())
    assert (
        conn.execute("SELECT count(*) FROM mail_category_corrections").fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT category FROM mail_summary_membership WHERE ordinal=4"
        ).fetchone()[0]
        == "build_ci"
    )
