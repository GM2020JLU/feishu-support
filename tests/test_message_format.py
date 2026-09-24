from k3_support.message_format import (
    format_feishu_ai_message,
    format_feishu_notice_text,
    is_feishu_ai_message,
)


def test_ai_marker_becomes_heading_and_keeps_markdown_body():
    rendered = format_feishu_ai_message(
        "[AI 自动回复]\n\n**答复**\n\n请看 [文档](https://example.invalid/doc)"
    )

    assert rendered == (
        "### AI 自动回复\n\n**答复**\n\n"
        "请看 [文档](https://example.invalid/doc)"
    )


def test_inline_ai_marker_is_split_from_body():
    assert format_feishu_ai_message("[AI 助手处理中] 已收到。") == (
        "### AI 助手处理中\n\n已收到。"
    )


def test_clarification_heading_is_clearer():
    assert format_feishu_ai_message("[AI 助手确认]\n\n请提供版本") == (
        "### AI 助手需要确认\n\n请提供版本"
    )


def test_owner_notice_uses_heading_bold_fields_and_escapes_values():
    rendered = format_feishu_notice_text(
        "K3 P0 严重故障提醒\nCase: K3-1\n标题: 启动失败 [待确认]"
    )

    assert rendered == (
        "### K3 P0 严重故障提醒\n\n"
        "- **Case：** K3-1\n"
        "- **标题：** 启动失败 \\[待确认\\]"
    )


def test_ai_provenance_recognizes_new_and_legacy_markers_only_at_start():
    assert is_feishu_ai_message("### AI 自动回复\n\n答复")
    assert is_feishu_ai_message("[AI 助手确认]请补充版本")
    assert is_feishu_ai_message("### AI 助手需要确认\n\n请补充版本")
    assert not is_feishu_ai_message("请解释 ### AI 自动回复 是什么")
