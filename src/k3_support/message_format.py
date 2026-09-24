from __future__ import annotations

import re
from collections.abc import Iterable

_AI_LABELS = {
    "AI 自动回复": "AI 自动回复",
    "AI 助手处理中": "AI 助手处理中",
    "AI 助手确认": "AI 助手需要确认",
}


def is_feishu_ai_message(text: str) -> bool:
    """Recognize current Markdown and legacy plain-text AI provenance markers."""
    stripped = text.lstrip()
    labels = (*_AI_LABELS, *_AI_LABELS.values())
    return any(
        stripped.startswith(f"[{label}]")
        or re.match(rf"^#{{1,6}}\s+{re.escape(label)}(?:\s|$)", stripped)
        is not None
        for label in labels
    )


def escape_lark_markdown(value: str) -> str:
    """Escape untrusted text used inside a Feishu Markdown message."""
    return (
        value.replace("\\", "\\\\")
        .replace("*", "\\*")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("`", "\\`")
    )


def format_feishu_ai_message(text: str) -> str:
    """Turn the stable AI marker into a readable Feishu Markdown heading."""
    stripped = text.strip()
    for marker, title in _AI_LABELS.items():
        match = re.match(rf"^\[{re.escape(marker)}\]\s*", stripped)
        if match is None:
            continue
        body = stripped[match.end() :].lstrip()
        heading = f"### {title}"
        return f"{heading}\n\n{body}" if body else heading
    return stripped


def format_feishu_notice(
    title: str,
    *,
    fields: Iterable[tuple[str, str]] = (),
    details: Iterable[str] = (),
) -> str:
    """Render a restrained owner notice with escaped labels and values."""
    output = [f"### {escape_lark_markdown(title.strip() or 'K3 提醒')}"]
    rows = [
        f"- **{escape_lark_markdown(label.strip())}：** {escape_lark_markdown(value.strip())}"
        for label, value in fields
        if label.strip() and value.strip()
    ]
    rows.extend(
        f"- {escape_lark_markdown(detail.strip())}"
        for detail in details
        if detail.strip()
    )
    if rows:
        output.extend(("", *rows))
    return "\n".join(output)


def format_feishu_notice_text(text: str) -> str:
    """Convert the legacy title-and-fields notice representation to Markdown."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return format_feishu_notice("K3 提醒")
    fields: list[tuple[str, str]] = []
    details: list[str] = []
    for line in lines[1:]:
        parts = re.split(r"[:：]", line, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            fields.append((parts[0], parts[1]))
        else:
            details.append(line)
    return format_feishu_notice(lines[0], fields=fields, details=details)
