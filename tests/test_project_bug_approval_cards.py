from k3_support.control import ControlMessage
from k3_support.feishu_card_bindings import bind, issue, resolve
from k3_support.hermes_plugin import _format_receipt
from k3_support.hermes_plugin.feishu_cards import NAMESPACE, render


def test_project_bug_approval_render_and_index_binding(conn):
    message = ControlMessage("owner", "approval-chat", "open-1", "bug close")
    result = {
        "command": "project_bug_approval",
        "text": "Complete preview\nEvidence is current.",
        "commands": [
            "批准此关闭审批\nbug approve-close K3-123 sha256:abcd",
            "拒绝此关闭审批\nbug deny-close K3-123 sha256:abcd",
        ],
    }
    issued = issue(conn, message, result)
    card = render(issued)

    assert issued["card_id"]
    assert card["header"]["title"]["content"] == "关闭审批"
    assert card["body"]["elements"][0]["text"]["content"] == result["text"]
    buttons = [item for item in card["body"]["elements"] if item["tag"] == "button"]
    assert [item["text"]["content"] for item in buttons] == [
        "批准此关闭审批",
        "拒绝此关闭审批",
    ]
    callback = buttons[0]["behaviors"][0]["value"][NAMESPACE]
    assert callback == f"card-action {issued['card_id']} 0"

    bind(conn, message, issued["card_id"], "om_approval")
    click = ControlMessage(
        "owner",
        "approval-chat",
        "click-1",
        f"card-action {issued['card_id']} 0",
        source_card_message_id="om_approval",
    )
    assert resolve(conn, click, issued["card_id"], "0") == (
        "bug approve-close K3-123 sha256:abcd"
    )
    assert resolve(conn, click, issued["card_id"], "1") == (
        "bug deny-close K3-123 sha256:abcd"
    )


def test_stale_project_bug_approval_can_render_deny_only_and_text_fallback():
    value = {
        "command": "project_bug_approval",
        "card_id": "fcc_stale",
        "text": "Complete stale preview",
        "commands": ["拒绝此关闭审批\nbug deny-close K3-123 sha256:old"],
    }
    card = render(value)
    buttons = [item for item in card["body"]["elements"] if item["tag"] == "button"]
    assert len(buttons) == 1
    assert buttons[0]["text"]["content"] == "拒绝此关闭审批"
    assert _format_receipt(True, value) == (
        "Complete stale preview\n\n"
        "拒绝此关闭审批\nbug deny-close K3-123 sha256:old"
    )
