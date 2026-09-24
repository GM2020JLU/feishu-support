"""Complete meeting text and existing evidence; no calendar transport."""

import json

from .calendar import historical_meeting_preview
from .meeting_preview import _render
from .meeting_recovery import meeting_recovery_report


def meeting_view(conn, config, approval_id):
    linked = conn.execute(
        "SELECT preview_id FROM meeting_previews WHERE approval_id=?", (approval_id,)
    ).fetchone()
    if linked is None:
        return None
    preview, action = historical_meeting_preview(conn, linked[0])
    first = _render(preview, action, 1)["preview"]
    panels = [
        _render(preview, action, page)["preview"]
        for page in range(1, first["page_count"] + 1)
    ]
    confirm = next(
        (
            button["text"]
            for button in panels[-1]["buttons"]
            if button["callback_data"].startswith("mt:a:")
        ),
        None,
    )
    report = meeting_recovery_report(conn, config, preview_id=linked[0])
    queued = conn.execute(
        "SELECT state,result_json FROM meeting_dispatch_queue WHERE preview_id=?",
        (linked[0],),
    ).fetchone()
    text = "".join(panel["plain_text"] for panel in panels)
    if action.get("mail_source"):
        source = action["mail_source"]
        text += "\n\n邮件来源：" + source["message_id"] + "\n草稿版本：" + str(source["draft_revision"])
    text += "\n\n已存创建与邀请记录（只读）：\n" + json.dumps(
        report, ensure_ascii=False, indent=2
    )
    if queued:
        text += "\n后台派发记录：\n" + json.dumps(
            dict(queued), ensure_ascii=False, indent=2
        )
    return {"text": text, "confirm_label": confirm}
