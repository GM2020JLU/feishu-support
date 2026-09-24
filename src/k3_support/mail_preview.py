"""Authenticated drill-down over frozen mail metadata, never a mailbox reader."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .approvals import verify_control_identity
from .knowledge_preview import _pages
from .mail_snapshot import (
    CATEGORY_LABELS,
    MailSnapshotError,
    classification_review_digest,
    correct_category,
    query_summary,
)

CATEGORY_CODES = dict(zip("0123456789", CATEGORY_LABELS, strict=True))
ATTENTION_LABELS = {
    "information": "知会",
    "action_required": "需行动",
    "blocked": "阻塞/失败",
    "waiting": "等待跟进",
}
PAGE_SIZE = 4


def summary_entry_button(
    digest_id: str, membership_digest: str | None
) -> dict[str, Any]:
    if not re.fullmatch(r"mdg_[0-9a-f]{32}", digest_id):
        raise MailSnapshotError("invalid mail summary ID")
    return {
        "text": "展开分类与待行动",
        "callback_data": f"ml:h:{digest_id[4:]}:{(membership_digest or 'legacy')[:16]}",
        "row": 0,
    }


def _button(digest_id, operation, argument, text, row=0):
    callback = f"ml:{operation}:{digest_id[4:]}:{argument}"
    if len(callback.encode()) > 64:
        raise MailSnapshotError("mail preview callback exceeds Telegram limit")
    return {"text": text, "callback_data": callback, "row": row}


def _number(value):
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = ""
    while value:
        value, rem = divmod(value, 36)
        result = alphabet[rem] + result
    return result or "0"


def _int(value):
    if not re.fullmatch("[0-9a-z]{1,6}", value):
        raise MailSnapshotError("invalid mail preview coordinate")
    return int(value, 36)


def _home_button(run):
    return _button(
        run["digest_id"],
        "h",
        (run["membership_digest"] or "legacy")[:16],
        "返回摘要分类",
        7,
    )


def _return_button(run, origin):
    match = re.fullmatch(r"([lt])([anw0-9])([0-9a-z]{1,6})", origin)
    if match:
        operation, filter_code, page = match.groups()
        return _button(
            run["digest_id"], operation, f"{filter_code}.{page}", "返回列表原页", 6
        )
    match = re.fullmatch(
        r"g([0-9a-z]{1,6})-([0-9a-z]{1,6})-([lt][anw0-9][0-9a-z]{1,6})", origin
    )
    if match:
        ordinal, page, list_origin = match.groups()
        return _button(
            run["digest_id"], "g", f"{ordinal}.{page}.{list_origin}", "返回线程原页", 6
        )
    raise MailSnapshotError("invalid mail preview return coordinate")


def _context(conn, config, *, user_id, chat_id, prompt_message_id, digest_id):
    verify_control_identity(config, user_id, chat_id)
    row = conn.execute(
        """SELECT r.*,o.payload_json,o.remote_message_id,o.destination,
                        o.state AS delivery_state,o.channel,o.action_type FROM mail_digest_runs r
                        JOIN outbox o ON o.outbox_id=r.telegram_outbox_id WHERE r.digest_id=?""",
        (digest_id,),
    ).fetchone()
    if not row:
        raise MailSnapshotError("mail preview is not bound to a delivered summary")
    run = dict(row)
    payload = json.loads(run["payload_json"])
    if (
        run["delivery_state"] != "delivered"
        or run["channel"] != "telegram"
        or run["action_type"] != "mail_summary"
        or str(run["remote_message_id"]) != prompt_message_id
        or run["destination"] != f"telegram:{chat_id}"
        or run["telegram_destination"] != f"telegram:{chat_id}"
        or payload.get("summary_id") != digest_id
        or "mail_membership_digest" not in payload
        or payload["mail_membership_digest"] != run["membership_digest"]
        or summary_entry_button(digest_id, run["membership_digest"])
        not in (payload.get("buttons") or [])
    ):
        raise MailSnapshotError(
            "mail preview prompt or snapshot binding does not match"
        )
    snapshot = query_summary(
        conn, digest_id=digest_id, page_size=1, expected_digest=run["membership_digest"]
    )
    members = []
    if snapshot["state"] != "legacy_membership_unavailable":
        for item in conn.execute(
            """SELECT m.ordinal,m.message_id,m.category,m.attention,m.thread_id,m.received_epoch_ms,
                                    m.metadata_json,c.category AS current_category,c.updated_at,l.message_app_link
                                    FROM mail_summary_membership m LEFT JOIN mail_catalog_items c USING(message_id)
                                    LEFT JOIN mail_digest_links l ON l.digest_id=m.digest_id AND l.message_id=m.message_id
                                    WHERE m.digest_id=? ORDER BY m.ordinal""",
            (digest_id,),
        ):
            value = dict(item)
            metadata = json.loads(value.pop("metadata_json"))
            # Explicit metadata allowlist: never expose cached body excerpts.
            value.update(
                {
                    key: metadata.get(key)
                    for key in (
                        "subject",
                        "sender",
                        "classification_source",
                        "needs_classification_review",
                    )
                }
            )
            members.append(value)
    return run, snapshot, members


def _panel(text, buttons, *, page=1, page_count=1, operation="show"):
    return {
        "command": "mail_preview",
        "operation": operation,
        "preview": {
            "text": text,
            "parse_mode": "HTML",
            "buttons": buttons,
            "page": page,
            "page_count": page_count,
        },
    }


def _plain(text):
    import html

    return html.escape(str(text))


def _category_label(category):
    return CATEGORY_LABELS.get(category, "未知（当前分类记录不可用）")


def _home(run, snapshot, members):
    if snapshot["state"] == "legacy_membership_unavailable":
        return _panel(
            "<b>历史邮件摘要</b>\n"
            + _plain(snapshot["reason"])
            + f"\n历史计数：{snapshot['historical_count']} 封。\n不会重新读取邮箱来猜测成员。",
            [],
        )
    category_counts = Counter(item["category"] for item in members)
    needs = sum(item["attention"] in {"blocked", "action_required"} for item in members)
    lines = [
        "邮件摘要分类",
        f"范围：{run['range_start']} — {run['range_end']}",
        f"本摘要 {snapshot['message_count']} 封 / {snapshot['thread_count']} 个线程（历史快照）",
        "分类采用摘要生成时的记录；本次展开不追加读取正文。",
    ]
    buttons = []
    for index, (code, category) in enumerate(CATEGORY_CODES.items()):
        label = CATEGORY_LABELS[category]
        count = category_counts[category]
        buttons.append(
            _button(run["digest_id"], "l", f"{code}.1", f"{label} {count}", index // 3)
        )
    buttons.extend(
        [
            _button(run["digest_id"], "l", "n.1", f"待行动 {needs}", 4),
            _button(run["digest_id"], "l", "w.1", "等待跟进", 4),
            _button(run["digest_id"], "l", "a.1", "逐封查看", 5),
            _button(run["digest_id"], "t", "a.1", "按线程折叠", 5),
        ]
    )
    return _panel("<b>邮件摘要</b>\n" + _plain("\n".join(lines)), buttons)


def _filtered(members, filter_code):
    if filter_code == "a":
        return members
    if filter_code == "n":
        return [
            item
            for item in members
            if item["attention"] in {"blocked", "action_required"}
        ]
    if filter_code == "w":
        return [item for item in members if item["attention"] == "waiting"]
    if filter_code in CATEGORY_CODES:
        return [
            item for item in members if item["category"] == CATEGORY_CODES[filter_code]
        ]
    raise MailSnapshotError("invalid mail preview filter")


def _member(members, ordinal):
    match = next((item for item in members if item["ordinal"] == ordinal), None)
    if match is None:
        raise MailSnapshotError("mail item is not in this summary")
    return match


def _paginate(items, page):
    total = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    if not 1 <= page <= total:
        raise MailSnapshotError("mail preview page is out of range")
    return items[(page - 1) * PAGE_SIZE : page * PAGE_SIZE], total


def _thread_key(item):
    return (
        ("thread", item["thread_id"])
        if item["thread_id"]
        else ("message", item["message_id"])
    )


def _listing(run, snapshot, members, operation, argument):
    parts = argument.split(".")
    if operation == "g":
        if len(parts) != 3:
            raise MailSnapshotError("invalid thread page")
        ordinal, page_text, origin = parts
        representative = _member(members, _int(ordinal))
        filtered = [
            item for item in members if _thread_key(item) == _thread_key(representative)
        ]
        return_button = _return_button(run, origin)
        title = "线程内全部摘要成员（包含不同失败）"
        page = _int(page_text)
        display = filtered
        current_origin = f"g{ordinal}-{page_text}-{origin}"
    else:
        if len(parts) != 2:
            raise MailSnapshotError("invalid mail list page")
        filter_code, page_text = parts
        page = _int(page_text)
        filtered = _filtered(members, filter_code)
        title = {"a": "全部", "n": "待行动", "w": "等待跟进"}.get(
            filter_code, CATEGORY_LABELS.get(CATEGORY_CODES.get(filter_code, ""), "")
        )
        display = []
        if operation == "t":
            groups = {}
            for item in filtered:
                if item["attention"] != "information":
                    display.append(item)
                else:
                    group = groups.setdefault(_thread_key(item), [])
                    group.append(item)
            display.extend(
                {**group[0], "folded_count": len(group)} for group in groups.values()
            )
            display.sort(key=lambda item: item["ordinal"])
            title += " · 线程折叠（仅合并知会邮件）"
        else:
            display = filtered
        current_origin = f"{operation}{filter_code}{page_text}"
        return_button = _home_button(run)
    selected, page_count = _paginate(display, page)
    lines = [
        title,
        f"本摘要 {snapshot['message_count']} 封 / {snapshot['thread_count']} 个线程；本视图 {len(filtered)} 封。",
        f"第 {page}/{page_count} 页 · 折叠仅影响显示，计数与各失败条目保持完整。",
    ]
    buttons = []
    for number, item in enumerate(selected, 1):
        label = ATTENTION_LABELS.get(item["attention"], "未分类")
        historical = CATEGORY_LABELS[item["category"]]
        changed = (
            ""
            if item["current_category"] == item["category"]
            else f"；现分类 {_category_label(item['current_category'])}"
        )
        lines.extend(
            [
                "",
                f"{number}. [{label}] {str(item['subject'] or '（无主题）')[:160]}",
                f"摘要分类 {historical}{changed} · {str(item['sender'] or '未知发件人')[:80]}",
            ]
        )
        ordinal = _number(item["ordinal"])
        if "folded_count" in item:
            lines.append(
                f"已合并 {item['folded_count']} 封知会；展开可看同线程全部邮件。"
            )
            buttons.append(
                _button(
                    run["digest_id"],
                    "g",
                    f"{ordinal}.1.{current_origin}",
                    f"{number}. 展开线程",
                    number - 1,
                )
            )
        else:
            buttons.append(
                _button(
                    run["digest_id"],
                    "d",
                    f"{ordinal}.1.{current_origin}",
                    f"{number}. 查看邮件",
                    number - 1,
                )
            )
    if not selected:
        lines.append("该视图没有邮件；不会向邮箱追加读取。")
    for target, label in ((page - 1, "上一页"), (page + 1, "下一页")):
        if 1 <= target <= page_count:
            arg = f"{parts[0]}.{_number(target)}" + (
                f".{parts[2]}" if operation == "g" else ""
            )
            buttons.append(_button(run["digest_id"], operation, arg, label, 4))
    if operation in {"l", "t"}:
        other = "t" if operation == "l" else "l"
        buttons.append(
            _button(
                run["digest_id"],
                other,
                f"{parts[0]}.1",
                "线程折叠" if other == "t" else "逐封显示",
                5,
            )
        )
    buttons.append(return_button)
    return _panel(
        "<b>摘要邮件列表</b>\n" + _plain("\n".join(lines)),
        buttons,
        page=page,
        page_count=page_count,
    )


def _detail(run, members, argument):
    parts = argument.split(".")
    if len(parts) != 3:
        raise MailSnapshotError("invalid mail detail page")
    ordinal, page_text, origin = parts
    item = _member(members, _int(ordinal))
    timestamp = (
        datetime.fromtimestamp(item["received_epoch_ms"] / 1000, UTC)
        .astimezone(ZoneInfo(run["timezone"]))
        .isoformat()
    )
    lines = [
        "主题：" + str(item["subject"] or "（无主题）"),
        "发件人：" + str(item["sender"] or "未知发件人"),
        "时间：" + timestamp,
        "邮件 ID：" + item["message_id"],
        "线程 ID：" + str(item["thread_id"] or "未记录"),
        "摘要时类别：" + CATEGORY_LABELS[item["category"]],
        "当前类别：" + _category_label(item["current_category"]),
        "摘要时关注级别：" + ATTENTION_LABELS[item["attention"]],
        "分类来源（摘要记录）：" + str(item["classification_source"] or "未记录"),
        "是否需复核分类：" + ("是" if item["needs_classification_review"] else "否"),
        "打开原邮件："
        + str(item["message_app_link"] or "未准备链接；本次不会自动分享或读取正文。"),
        "这里只展示已保存元数据，未读取邮件正文。",
    ]
    pages = _pages("\n".join(lines))
    page = _int(page_text)
    if not 1 <= page <= len(pages):
        raise MailSnapshotError("mail preview page is out of range")
    buttons = []
    for target, label in ((page - 1, "上一页"), (page + 1, "下一页")):
        if 1 <= target <= len(pages):
            buttons.append(
                _button(
                    run["digest_id"],
                    "d",
                    f"{ordinal}.{_number(target)}.{origin}",
                    label,
                    0,
                )
            )
    if item["updated_at"]:
        buttons.append(_button(run["digest_id"], "c", ordinal, "纠正当前分类", 1))
    buttons.extend([_return_button(run, origin), _home_button(run)])
    return _panel(
        f"<b>邮件元数据 · {page}/{len(pages)} 页</b>\n\n" + pages[page - 1],
        buttons,
        page=page,
        page_count=len(pages),
    )


def _correction(conn, run, members, operation, argument, *, user_id, external_id):
    parts = argument.split(".")
    expected_parts = {"c": 1, "p": 2, "a": 3}[operation]
    if len(parts) != expected_parts:
        raise MailSnapshotError("invalid mail correction request")
    ordinal = parts[0]
    item = _member(members, _int(ordinal))
    default_origin = "la" + _number(item["ordinal"] // PAGE_SIZE + 1)
    back = _button(
        run["digest_id"], "d", f"{ordinal}.1.{default_origin}", "取消，返回本邮件", 6
    )
    if not item["updated_at"]:
        raise MailSnapshotError("mail catalog message not found")
    subject = str(item["subject"] or "（无主题）")
    subject_preview = subject[:300] + (
        "…（完整主题见邮件元数据）" if len(subject) > 300 else ""
    )
    text = f"邮件：{subject_preview}\n摘要原分类：{CATEGORY_LABELS[item['category']]}\n预览时当前分类：{CATEGORY_LABELS[item['current_category']]}\n只纠正当前分类；历史摘要和成员计数保持不变。"
    if operation == "c":
        buttons = [
            _button(run["digest_id"], "p", f"{ordinal}.{code}", label, index // 3)
            for index, (code, category) in enumerate(CATEGORY_CODES.items())
            for label in [CATEGORY_LABELS[category]]
        ]
        return _panel("<b>选择目标分类</b>\n" + _plain(text), buttons + [back])
    category = CATEGORY_CODES.get(parts[1])
    if category is None:
        raise MailSnapshotError("invalid mail correction category")
    current_digest = classification_review_digest(conn, item["message_id"])
    if operation == "a":
        if parts[2] != current_digest[:16]:
            raise MailSnapshotError(
                "mail classification changed; refresh before correcting"
            )
        # The core rechecks this full digest inside its write transaction. This
        # prevents a scan/correction race, including same-timestamp ABA changes.
        result = correct_category(
            conn,
            message_id=item["message_id"],
            category=category,
            actor_id=user_id,
            reason="操作者通过邮件摘要卡片预览并明确确认分类纠正",
            expected_updated_at=item["updated_at"],
            expected_current_digest=current_digest,
            external_id=external_id,
        )
        return _panel(
            "<b>已纠正当前分类</b>\n"
            + _plain(
                text
                + f"\n新的当前分类：{CATEGORY_LABELS[category]}\n没有改写历史摘要。"
            ),
            [
                _button(
                    run["digest_id"],
                    "d",
                    f"{ordinal}.1.{default_origin}",
                    "查看更新后的邮件分类",
                    0,
                ),
                _home_button(run),
            ],
            operation="corrected",
        ) | {"correction": result}
    confirmation = _button(
        run["digest_id"],
        "a",
        f"{ordinal}.{parts[1]}.{current_digest[:16]}",
        "确认纠正",
        0,
    )
    return _panel(
        "<b>请确认分类纠正</b>\n"
        + _plain(
            text
            + f"\n目标分类：{CATEGORY_LABELS[category]}\n审核版本：{current_digest[:16]}"
        ),
        [confirmation, back],
        operation="correction_preview",
    )


def route(
    conn,
    config,
    *,
    user_id: str,
    chat_id: str,
    prompt_message_id: str,
    callback_data: str,
    external_id: str,
):
    match = re.fullmatch(
        r"ml:([hltgdcpa]):([0-9a-f]{32}):([A-Za-z0-9_.-]{1,32})", callback_data
    )
    if match is None or len(callback_data.encode()) > 64 or not external_id:
        raise MailSnapshotError("invalid mail preview callback")
    operation, identifier, argument = match.groups()
    digest_id = "mdg_" + identifier
    # Navigation gets a coherent read-only snapshot; correction commits only in
    # correct_category, after this read snapshot is released.
    conn.execute("SAVEPOINT mail_preview_read")
    try:
        run, snapshot, members = _context(
            conn,
            config,
            user_id=user_id,
            chat_id=chat_id,
            prompt_message_id=prompt_message_id,
            digest_id=digest_id,
        )
        if operation == "h":
            if argument != (run["membership_digest"] or "legacy")[:16]:
                raise MailSnapshotError("mail summary snapshot changed")
            return _home(run, snapshot, members)
        if snapshot["state"] == "legacy_membership_unavailable":
            raise MailSnapshotError("legacy summary membership is unavailable")
        if operation in {"l", "t", "g"}:
            return _listing(run, snapshot, members, operation, argument)
        if operation == "d":
            return _detail(run, members, argument)
        if operation in {"c", "p"}:
            return _correction(
                conn,
                run,
                members,
                operation,
                argument,
                user_id=user_id,
                external_id=external_id,
            )
    finally:
        conn.execute("RELEASE mail_preview_read")
    return _correction(
        conn,
        run,
        members,
        operation,
        argument,
        user_id=user_id,
        external_id=external_id,
    )
