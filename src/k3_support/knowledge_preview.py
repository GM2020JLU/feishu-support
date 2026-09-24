"""Read-only, content-bound knowledge pages for the operator's Telegram inbox."""

from __future__ import annotations

import html
import json
import re
import sqlite3
from urllib.parse import urlsplit
from typing import Any

from .ids import digest


class KnowledgePreviewError(ValueError):
    pass


def _source_links(mappings):
    """Navigation only: never fetch a source or infer its access permissions."""
    result, seen = [], set()
    for source in mappings:
        url = source.get('url')
        if (not isinstance(url, str) or len(url) > 4096
                or any(character.isspace() or ord(character) < 32 for character in url)
                or '\\' in url):
            continue
        try:
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None):
                continue
            parsed.port  # Reject malformed ports, not merely malformed hosts.
        except ValueError:
            continue
        if url not in seen:
            seen.add(url)
            result.append({'title': source.get('title') or '来源文档', 'url': url})
    return result


_SCOPE_LABELS = {
    "product": "产品",
    "component": "组件",
    "subcomponent": "子组件",
    "basis": "适用依据",
    "boards": "板型",
    "hardware_revisions": "硬件版本",
    "software_versions": "软件版本",
    "boot_stages": "启动阶段",
    "storage_media": "存储介质",
    "operating_systems": "操作系统",
}
_STATE_LABELS = {
    "candidate": "待审核",
    "approved": "已有审核记录",
    "stale": "需要复审",
    "retired": "已停用",
    "published": "已发布",
    "needs_review": "需要复审",
    "captured": "原始草稿",
    "structured": "已整理",
    "verified": "证据已核验",
}


def _display(value: Any) -> str:
    if value is None or value == "" or value == []:
        return "未记录"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def _pages(text: str) -> list[str]:
    """Bound escaped HTML and UTF-16 size without dropping a single character."""
    result: list[str] = []
    current: list[str] = []
    size = 0
    for character in text:
        escaped = html.escape(character)
        width = max(len(escaped), len(character.encode("utf-16-le")) // 2)
        if size + width > 2800:
            result.append("".join(current))
            current, size = [], 0
        current.append(escaped)
        size += width
    if current or not result:
        result.append("".join(current))
    return result


def knowledge_preview(
    conn: sqlite3.Connection,
    *,
    knowledge_id: str,
    page: int = 1,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    """Read current content only; callers must first verify the operator identity."""
    # A source refresh may commit while a page is being assembled. Read all
    # rows from one SQLite snapshot, without committing a caller's transaction.
    conn.execute("SAVEPOINT knowledge_preview_read")
    try:
        return _knowledge_preview_snapshot(
            conn, knowledge_id=knowledge_id, page=page, expected_digest=expected_digest
        )
    finally:
        conn.execute("RELEASE knowledge_preview_read")


def _knowledge_preview_snapshot(
    conn: sqlite3.Connection,
    *,
    knowledge_id: str,
    page: int,
    expected_digest: str | None,
) -> dict[str, Any]:
    if not re.fullmatch(r"knw_[A-Za-z0-9]{1,32}", knowledge_id):
        raise KnowledgePreviewError("invalid knowledge preview ID")
    row = conn.execute(
        """SELECT knowledge_id,title,status,answer_markdown,project,module,hardware,
                  software_version,applicability,disclosure_class,evidence_layers_json,
                  reviewed_by,reviewed_at,review_due_at,professional_revision_id,
                  source_digest,content_digest
             FROM knowledge_entries WHERE knowledge_id=?""",
        (knowledge_id,),
    ).fetchone()
    if row is None:
        raise KnowledgePreviewError("knowledge entry not found")
    knowledge = dict(row)
    mappings = [
        dict(item)
        for item in conn.execute(
            """SELECT ks.source_type,ks.stable_external_id,ks.url,ks.source_version,
                      ks.visibility,ks.claim,sr.title,sr.acl_json,
                      sr.source_version AS current_source_version,sr.content_digest
                 FROM knowledge_sources ks LEFT JOIN source_registry sr
                   ON sr.source_type=ks.source_type
                  AND sr.stable_external_id=ks.stable_external_id
                WHERE ks.knowledge_id=? ORDER BY ks.mapping_id""",
            (knowledge_id,),
        )
    ]
    professional = None
    metadata: dict[str, Any] = {}
    if knowledge["professional_revision_id"]:
        revision = conn.execute(
            "SELECT * FROM professional_knowledge_revisions WHERE revision_id=?",
            (knowledge["professional_revision_id"],),
        ).fetchone()
        if revision is None:
            raise KnowledgePreviewError(
                "knowledge professional revision is unavailable"
            )
        professional = dict(revision)
        metadata = json.loads(professional["payload_json"])

    content = metadata.get("content") or {}
    intent = metadata.get("intent") or {}
    scope = metadata.get("scope") or {}
    lines = [
        f"标题：{knowledge['title']}",
        f"状态：{_STATE_LABELS.get(knowledge['status'], knowledge['status'])}",
        f"审核人：{_display(knowledge['reviewed_by'])}",
        f"审核时间：{_display(knowledge['reviewed_at'])}",
        f"复审期限：{_display(knowledge['review_due_at'])}",
        f"可见范围：{knowledge['disclosure_class']}",
    ]
    if professional:
        state = professional["lifecycle_state"]
        lines.extend(
            [
                f"知识版本：{professional['stable_id']} · {professional['revision_number']}",
                f"专业知识状态：{_STATE_LABELS.get(state, state)}",
            ]
        )
    lines.extend(["", "适用范围"])
    if scope:
        lines.extend(
            f"{_SCOPE_LABELS.get(key, key)}：{_display(value)}"
            for key, value in scope.items()
        )
    else:
        for key, label in (
            ("project", "产品"),
            ("module", "组件"),
            ("hardware", "硬件"),
            ("software_version", "软件版本"),
            ("applicability", "适用条件"),
        ):
            lines.append(f"{label}：{_display(knowledge[key])}")
    lines.extend(
        [
            "",
            "前提与警告（请连同答案审核）",
            f"前提：{_display(content.get('prerequisites'))}",
            f"警告：{_display(content.get('warnings'))}",
            f"必须确认的信息：{_display(intent.get('required_entities'))}",
            f"不适用条件：{_display(intent.get('negative_constraints'))}",
            f"已有验证层级：{_display(json.loads(knowledge['evidence_layers_json']))}",
            "",
            "答案全文",
            str(knowledge["answer_markdown"]),
            "",
            "来源与定位",
        ]
    )
    sources = metadata.get("sources") or mappings
    if not sources:
        lines.append("未记录来源；查看不表示答案已获得审核或发布资格。")
    for index, source in enumerate(sources, 1):
        lines.extend(
            [
                f"{index}. {_display(source.get('title') or source.get('stable_external_id'))}",
                f"链接：{_display(source.get('url'))}",
                f"版本：{_display(source.get('version') or source.get('source_version'))}",
                f"定位：{_display(source.get('locator') or source.get('stable_external_id'))}",
                f"披露方式：{_display(source.get('share_mode') or source.get('visibility'))}",
            ]
        )
        if source.get("claim"):
            lines.append(f"支持的结论：{source['claim']}")
    if content.get("failure_branches") or content.get("rollback"):
        lines.extend(
            [
                "",
                "异常与恢复",
                f"失败分支：{_display(content.get('failure_branches'))}",
                f"恢复方式：{_display(content.get('rollback'))}",
            ]
        )
    text = "\n".join(lines)
    # Include current mappings and professional lifecycle as well as displayed
    # text. A source or publication change invalidates already-issued pages.
    fingerprint = digest(
        {
            "format": 1,
            "knowledge": knowledge,
            "mappings": mappings,
            "professional": professional,
            "text": text,
        }
    )[:16]
    if expected_digest is not None and expected_digest != fingerprint:
        raise KnowledgePreviewError(
            "knowledge preview is stale; open knowledge show again"
        )
    pages = _pages(text)
    if len(pages) > 9999 or isinstance(page, bool) or not 1 <= page <= len(pages):
        raise KnowledgePreviewError("knowledge preview page is out of range")
    buttons = []
    for target, label in ((page - 1, "上一页"), (page + 1, "下一页")):
        if 1 <= target <= len(pages):
            buttons.append(
                {
                    "text": label,
                    "callback_data": f"knp:{knowledge_id}:{fingerprint}:{target}",
                    "row": 0,
                }
            )
    return {
        "command": "knowledge",
        "operation": "show",
        "knowledge": knowledge,
        "preview": {
            "text": (
                f"<b>知识预览 · {page}/{len(pages)} 页</b>\n"
                "只读查看；不会批准、发布或开启自动回复。\n"
                "请查看全部页面，答案与适用范围、警告共同构成审核内容。\n\n"
                + pages[page - 1]
            ),
            "parse_mode": "HTML",
            "plain_text": html.unescape(pages[page - 1]),
            "buttons": buttons,
            "page": page,
            "page_count": len(pages),
            "content_digest": fingerprint,
            "source_links": _source_links(mappings),
        },
    }
