"""Hermes gateway adapter for deterministic K3 support control commands.

This module is copied as a self-contained Hermes user plugin.  Keep it limited
to the Python standard library: it runs in Hermes' virtual environment, not the
K3 support control-plane virtual environment.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import re
import shlex
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

LOGGER = logging.getLogger(__name__)
PLUGIN_NAME = "k3-support-control"
DEFAULT_TIMEOUT_SECONDS = 10

# Match only unmistakable control-plane messages.  Malformed commands with an
# unmistakable prefix are still intercepted and rejected by the authoritative
# CLI; they must never fall through to an LLM that may reinterpret them.
_CONTROL_PREFIX = re.compile(
    r"^(?:"
    r"/feishu$|"
    r"approval(?:\s|$)|"
    r"mode(?:-action)?(?:\s|$)|"
    r"card-(?:bind|action)(?:\s|$)|"
    r"bug(?:\s|$)|"
    r"workbench(?:\s|$)|"
    r"workbench-nav(?:\s|$)|"
    r"case-action(?:\s|$)|"
    r"mail-view(?:\s|$)|"
    r"meeting-view(?:\s|$)|"
    r"meeting-recovery(?:-scope)?(?:\s|$)|"
    r"remote-recovery(?:\s|$)|"
    r"approve\s+(?:(?:board|push|meeting)\s+)?\S+\b|"
    r"deny\s+apr_[A-Za-z0-9]+\b|"
    r"(?:pause|resume|takeover|cancel|resolve|reopen|status|claim|suggest-only|delegate)\s+K3-[A-Za-z0-9_-]+\b|"
    r"knowledge\s+(?:show|approve|return|retire)\s+knw_[A-Za-z0-9]+\b|"
    r"knowledge\s+feedback\s+(?:helpful|incorrect|incomplete|sensitive)\s+(?:K3-[A-Za-z0-9_-]+|knw_[A-Za-z0-9]+)\b"
    r")",
    re.IGNORECASE,
)


class PluginConfigurationError(RuntimeError):
    """The adapter cannot safely reach the authoritative control plane."""


def _hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def _runtime_config_path() -> Path:
    configured = os.environ.get("K3_SUPPORT_CONTROL_PLUGIN_CONFIG")
    if configured:
        return Path(configured).expanduser()
    return _hermes_home() / "k3-support" / "control-plugin.json"


def _load_runtime_config() -> dict[str, Any]:
    path = _runtime_config_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PluginConfigurationError("控制插件尚未配置") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginConfigurationError("控制插件配置不可读") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise PluginConfigurationError("控制插件配置版本无效")
    cli = value.get("control_cli")
    control_config = value.get("control_config")
    if not isinstance(cli, str) or not Path(cli).is_absolute():
        raise PluginConfigurationError("control_cli 必须是绝对路径")
    if not isinstance(control_config, str) or not Path(control_config).is_absolute():
        raise PluginConfigurationError("control_config 必须是绝对路径")
    cli_path = Path(cli)
    config_path = Path(control_config)
    if not cli_path.is_file() or not os.access(cli_path, os.X_OK):
        raise PluginConfigurationError("控制程序不存在或不可执行")
    if not config_path.is_file():
        raise PluginConfigurationError("控制面配置不存在")
    try:
        timeout = int(value.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError) as exc:
        raise PluginConfigurationError("timeout_seconds 必须是整数") from exc
    if not 1 <= timeout <= 30:
        raise PluginConfigurationError("timeout_seconds 必须在 1 到 30 秒之间")
    return {
        "control_cli": str(cli_path),
        "control_config": str(config_path),
        "timeout_seconds": timeout,
    }


def _platform_name(source: Any) -> str:
    platform = getattr(source, "platform", None)
    return str(getattr(platform, "value", platform) or "").lower()


def _is_control_message(text: str) -> bool:
    return bool(_CONTROL_PREFIX.match(text.strip()))


def _safe_error(stderr: str) -> str:
    lowered = stderr.lower()
    known = (
        (
            "mail preview prompt or snapshot binding",
            "这不是原始邮件摘要卡，或摘要快照已改变，请重新打开原卡",
        ),
        ("mail preview is not bound", "此邮件摘要尚无有效送达卡片"),
        ("mail classification changed", "当前分类已更新，请返回邮件重新预览纠正"),
        (
            "mail classification review changed",
            "当前分类已更新，请返回邮件重新预览纠正",
        ),
        (
            "meeting exact action changed",
            "会议时间、参会人或内容已改变，请重新生成完整预览",
        ),
        ("meeting callback is not bound", "这不是该会议的原始审批卡"),
        ("meeting approval is no longer pending", "会议审批已处理或过期；不会重复创建"),
        ("meeting preview is stale", "该会议审批属于旧的问题轮次，已不能执行"),
        (
            "meeting requires its complete preview",
            "请先打开会议完整预览，在最后一页确认",
        ),
        (
            "calendar create outcome is uncertain",
            "日历创建结果尚未确认，可能已经发出邀请；请人工核对原日历，不要重试",
        ),
        ("mail preview page is out of range", "邮件页码无效，请返回摘要分类"),
        ("mail summary snapshot changed", "邮件摘要快照不匹配，请重新打开原卡"),
        ("legacy summary membership", "旧摘要未保存逐封成员，不能重建历史分类"),
        ("stale case lifecycle", "Case 状态或权限已有更新，请重新打开详情"),
        ("current authority round", "此按钮属于旧轮次，请重新打开 Case 详情"),
        ("workbench snapshot is stale", "工作台已有更新，请点击刷新后重试"),
        ("workbench detail is stale", "Case 已有更新，请返回工作台重新查看"),
        ("workbench page is out of range", "工作台页码无效，请刷新后重试"),
        ("global control panel is stale", "这张总控卡已过期，请重新发送 /feishu"),
        (
            "knowledge preview is stale",
            "知识内容或来源已更新，请重新发送 knowledge show 查看最新版",
        ),
        ("knowledge preview page is out of range", "知识预览页码无效，请重新打开预览"),
        ("approval request expired", "审批请求已过期"),
        ("approval is expired", "审批请求已过期"),
        ("control identity mismatch", "控制身份不匹配"),
        ("approval digest mismatch", "审批摘要不匹配"),
        ("approval type does not match command", "审批类型与命令不匹配"),
        ("already leased", "board1 已被其他会话占用"),
        ("cleanup pending", "board1 上次会话仍在清理"),
        ("unsupported control command", "不支持的控制命令"),
        (
            "case callback is stale for the current conversation turn",
            "这张卡对应的对话已有更新；需要授权 AI 时请使用最新卡片",
        ),
        ("usage:", "控制命令格式错误"),
    )
    for marker, message in known:
        if marker in lowered:
            return message
    return "控制面拒绝了该命令；详细原因已写入本机日志"


def _run_control(event: Any) -> tuple[bool, dict[str, Any] | str]:
    source = getattr(event, "source", None)
    user_id = str(getattr(source, "user_id", None) or "")
    chat_id = str(getattr(source, "chat_id", None) or "")
    message_id = str(
        getattr(event, "message_id", None) or getattr(source, "message_id", None) or ""
    )
    if not user_id or not chat_id or not message_id:
        channel = "飞书" if _platform_name(source) == "feishu" else "Telegram"
        return False, f"消息缺少稳定的 {channel} 用户、会话或消息 ID"
    try:
        runtime = _load_runtime_config()
    except PluginConfigurationError as exc:
        return False, str(exc)
    argv = [
        runtime["control_cli"],
        "--config",
        runtime["control_config"],
        "control",
        "--control-user-id",
        user_id,
        "--control-chat-id",
        chat_id,
        "--message-id",
        message_id,
        "--text",
        str(getattr(event, "text", "")),
    ]
    if _platform_name(source) == "feishu":
        argv += ["--control-channel", "feishu"]
        if getattr(event, "control_reply_to", None):
            argv += ["--source-card-message-id", event.control_reply_to]
    try:
        result = subprocess.run(
            argv,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=runtime["timeout_seconds"],
        )
    except subprocess.TimeoutExpired:
        return False, "控制面处理超时；命令可使用同一消息安全重放"
    except (OSError, ValueError):
        LOGGER.exception("Unable to execute K3 support control CLI")
        return False, "无法启动控制程序"
    if result.returncode != 0:
        LOGGER.warning(
            "K3 support control command rejected: returncode=%s reason=%s",
            result.returncode,
            _safe_error(result.stderr),
        )
        return False, _safe_error(result.stderr)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        LOGGER.error("K3 support control CLI returned non-JSON output")
        return False, "控制面返回了无效回执"
    if not isinstance(payload, dict):
        return False, "控制面返回了无效回执"
    return True, payload


def _run_callback(
    *,
    user_id: str,
    chat_id: str,
    callback_query_id: str,
    prompt_message_id: str,
    action: str,
    target_id: str,
    target_type: str = "approval",
) -> tuple[bool, dict[str, Any] | str]:
    try:
        runtime = _load_runtime_config()
    except PluginConfigurationError as exc:
        return False, str(exc)
    argv = [
        runtime["control_cli"],
        "--config",
        runtime["control_config"],
        "control-callback",
        "--control-user-id",
        user_id,
        "--control-chat-id",
        chat_id,
        "--callback-query-id",
        callback_query_id,
        "--prompt-message-id",
        prompt_message_id,
        "--action",
        action,
    ]
    target_flag = {
        "approval": "--approval-id",
        "case": "--case-id",
        "panel": "--panel-id",
    }.get(target_type)
    if target_flag is None:
        return False, "控制按钮目标类型无效"
    argv.extend([target_flag, target_id])
    try:
        result = subprocess.run(
            argv,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=runtime["timeout_seconds"],
        )
    except subprocess.TimeoutExpired:
        return False, "控制面处理超时；可再次点击"
    except (OSError, ValueError):
        LOGGER.exception("Unable to execute K3 support callback CLI")
        return False, "无法启动控制程序"
    if result.returncode != 0:
        return False, _safe_error(result.stderr)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False, "控制面返回了无效回执"
    return (
        (True, payload)
        if isinstance(payload, dict)
        else (False, "控制面返回了无效回执")
    )


def _run_panel_bind(
    *,
    user_id: str,
    chat_id: str,
    command_message_id: str,
    prompt_message_id: str,
    panel_id: str,
) -> tuple[bool, dict[str, Any] | str]:
    try:
        runtime = _load_runtime_config()
    except PluginConfigurationError as exc:
        return False, str(exc)
    argv = [
        runtime["control_cli"],
        "--config",
        runtime["control_config"],
        "control-panel-bind",
        "--control-user-id",
        user_id,
        "--control-chat-id",
        chat_id,
        "--command-message-id",
        command_message_id,
        "--prompt-message-id",
        prompt_message_id,
        "--panel-id",
        panel_id,
    ]
    try:
        result = subprocess.run(
            argv,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=runtime["timeout_seconds"],
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return False, "总控面板绑定失败；请重新发送 /feishu"
    if result.returncode != 0:
        return False, _safe_error(result.stderr)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False, "控制面返回了无效回执"
    return (
        (True, payload)
        if isinstance(payload, dict)
        else (False, "控制面返回了无效回执")
    )


def _format_receipt(ok: bool, value: dict[str, Any] | str) -> str:
    if not ok:
        return f"❌ 控制命令未执行：{value}"
    payload = value if isinstance(value, dict) else {}
    command = str(payload.get("command") or "control")
    if command in {"feishu_mode", "approval_detail"}:
        lines = [str(payload.get("text") or "控制详情暂不可用")]
        lines.extend(str(item) for item in payload.get("commands") or [])
        return "\n\n".join(lines)
    if command == "project_bug_approval":
        commands = payload.get("commands") or []
        receipt = str(payload.get("text") or "Bug 关闭审批暂不可用")
        return receipt + (
            "\n\n" + "\n".join(str(item) for item in commands) if commands else ""
        )
    if command == "project_bug":
        return str(payload.get("text") or "Bug 控制暂不可用")
    if command in {"workbench", "mail_preview", "meeting_preview", "meeting_recovery"}:
        return str((payload.get("preview") or {}).get("text") or "工作台暂不可用")
    if command in {"resolve", "reopen"}:
        receipt = (
            "✅ 已由你标记解决"
            if command == "resolve"
            else "✅ 已重新打开，由你负责；未恢复旧任务或审批"
        )
        receipt += f"\nCase: {payload.get('case_id', '-')} · 版本 {payload.get('version', '-')} · 轮次 {payload.get('lifecycle_round', '-')}"
        in_flight = payload.get("in_flight_deliveries") or []
        if in_flight:
            receipt += (
                f"\n已有 {len(in_flight)} 次回复进入发送，结果尚未确认，无法保证撤回"
            )
        return receipt
    if command in {"approve", "deny"} and payload.get("replayed"):
        approval = payload.get("approval") or {}
        return (f"该审批已在 {approval.get('approver_channel', '-')} 处理，当前状态：{approval.get('status', '-')}。"
                f"\n未重复启动操作。\nApproval: {approval.get('approval_id', '-')}")
    if command == "approve":
        approval = payload.get("approval") or {}
        gate = payload.get("gate") or approval.get("approval_type") or "approval"
        if gate == "meeting" and payload.get("execution", {}).get("event_id"):
            execution = payload["execution"]
            return f"✅ 已创建飞书会议\n事件 ID：{execution['event_id']}\n{execution.get('app_link') or '创建回执没有提供链接'}"
        lines = [
            f"✅ 已批准 {gate}",
            f"Approval: {approval.get('approval_id', '-')}",
            f"Case: {approval.get('case_id', '-')}",
        ]
        if approval.get("session_id"):
            lines.append(f"Session: {approval['session_id']}")
        if approval.get("expires_at"):
            lines.append(f"有效期: {approval['expires_at']}")
        if payload.get("continuation"):
            lines.append("后续任务已进入确定性队列")
        return "\n".join(lines)
    if command == "deny":
        approval = payload.get("approval") or {}
        return (
            "✅ 已拒绝审批\n"
            f"Approval: {approval.get('approval_id', '-')}\n"
            f"Case: {approval.get('case_id', '-')}"
        )
    if command in {"pause", "resume", "takeover", "cancel"}:
        return (
            ("重复请求已核对，未再次执行\n" if payload.get("replayed") else f"✅ 已执行 {command}\n")
            + f"Case: {payload.get('case_id', '-')}\n"
            f"状态: {payload.get('state', '-')}\n"
            f"版本: {payload.get('version', '-')}"
        )
    if command in {"claim", "suggest_only", "delegate"}:
        turn = payload.get("turn") or {}
        labels = {
            "claim": "我来回复",
            "suggest_only": "只给我建议",
            "delegate": "交给 AI",
        }
        receipt = (
            f"✅ 已切换：{labels[command]}\n"
            f"Case: {payload.get('case_id', '-')}\n"
            f"沟通权: {turn.get('communication_owner', '-')} / {turn.get('communication_mode', '-')}"
        )
        if payload.get("retargeted_from_turn_id"):
            receipt += "\n已应用到该会话的最新消息"
        in_flight = payload.get("in_flight_deliveries") or []
        if in_flight:
            receipt += (
                f"\n已有 {len(in_flight)} 次回复进入发送，结果尚未确认，无法保证撤回"
            )
        return receipt
    if command == "status":
        case = payload.get("case") or {}
        receipt = (
            f"ℹ️ Case {case.get('case_id', '-')}\n"
            f"状态: {case.get('state', '-')}\n"
            f"严重度: {case.get('severity', '-')}\n"
            f"版本: {case.get('version', '-')}"
        )
        turns = case.get("turns") or []
        if turns:
            receipt += (
                f"\n沟通权: {turns[0].get('communication_owner', '-')} / "
                f"{turns[0].get('communication_mode', '-')}"
            )
        return receipt
    if command == "knowledge":
        if payload.get("operation") == "show":
            preview = payload.get("preview") or {}
            return str(preview.get("text") or "知识预览暂不可用；本次没有审核任何内容")
        knowledge = payload.get("knowledge") or {}
        return (
            f"✅ 知识条目已处理：{payload.get('operation', '-')}\n"
            f"Knowledge: {knowledge.get('knowledge_id', '-')}\n"
            f"状态: {knowledge.get('status', '-')}"
        )
    return f"✅ 控制命令已执行：{command}"


def _inline_keyboard(buttons: list[dict[str, Any]]) -> list[list[dict[str, str]]]:
    keyboard: list[list[dict[str, str]]] = []
    for button in buttons:
        text = str(button.get("text", ""))
        callback_data = str(button.get("callback_data", ""))
        if (
            not text
            or not callback_data.startswith(
                (
                    "k3a:",
                    "k3c:",
                    "fsc:",
                    "knp:",
                    "wb2:",
                    "wi2:",
                    "wd2:",
                    "wka2:",
                    "ml:",
                    "mt:",
                    "mr:",
                    "rr:",
                )
            )
            or len(callback_data.encode()) > 64
        ):
            raise PluginConfigurationError("button 数据无效")
        row_number = int(button.get("row", 0))
        if row_number < 0 or row_number > 7:
            raise PluginConfigurationError("button row 数据无效")
        while len(keyboard) <= row_number:
            keyboard.append([])
        keyboard[row_number].append({"text": text, "callback_data": callback_data})
    return [row for row in keyboard if row]


def _send_button_message(
    chat_id: str,
    text: str,
    buttons: list[dict[str, Any]],
    parse_mode: str | None = None,
    *, edit_message_id: str | None = None,
) -> str:
    if chat_id.startswith("telegram:"):
        chat_id = chat_id.split(":", 1)[1]
    fields = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": json.dumps(
            {"inline_keyboard": _inline_keyboard(buttons)}, ensure_ascii=False
        ),
    }
    if parse_mode is not None:
        if parse_mode != "HTML":
            raise PluginConfigurationError("不支持的 Telegram 文本格式")
        fields["parse_mode"] = parse_mode
    if edit_message_id is not None:
        fields['message_id'] = edit_message_id
    payload = urllib.parse.urlencode(fields).encode()
    token = _telegram_token()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{'editMessageText' if edit_message_id is not None else 'sendMessage'}", data=payload, method="POST"
    )
    proxy = os.environ.get("TELEGRAM_PROXY")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        if proxy
        else urllib.request.ProxyHandler()
    )
    try:
        with opener.open(request, timeout=20) as response:
            value = json.loads(response.read().decode())
    except Exception as exc:
        raise PluginConfigurationError("Telegram 按钮消息发送失败") from exc
    result = value.get("result") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("ok") is not True
        or not isinstance(result, dict)
        or not result.get("message_id")
        or (edit_message_id is not None and str(result.get('message_id')) != str(edit_message_id))
    ):
        raise PluginConfigurationError("Telegram 拒绝了按钮消息")
    return str(result["message_id"])


def _telegram_reply_markup(buttons: list[dict[str, Any]]) -> Any:
    keyboard = _inline_keyboard(buttons)
    try:
        telegram = __import__("telegram")
        return telegram.InlineKeyboardMarkup(
            [
                [telegram.InlineKeyboardButton(**button) for button in row]
                for row in keyboard
            ]
        )
    except (ImportError, AttributeError):
        # Test doubles and compatible adapters may accept the Bot API mapping.
        return {"inline_keyboard": keyboard}


async def _handle_knowledge_callback(query: Any, data: str) -> None:
    match = re.fullmatch(
        r"knp:(knw_[A-Za-z0-9]{1,32}):([0-9a-f]{16}):([1-9][0-9]{0,3})", data
    )
    if match is None:
        await query.answer(text="无效知识预览按钮", show_alert=True)
        return
    message = getattr(query, "message", None)
    user_id = str(getattr(getattr(query, "from_user", None), "id", "") or "")
    chat_id = str(getattr(message, "chat_id", "") or "")
    callback_id = str(getattr(query, "id", "") or "")
    if not all((user_id, chat_id, callback_id)):
        await query.answer(text="知识预览上下文不完整", show_alert=True)
        return
    knowledge_id, content_digest, page = match.groups()
    event = SimpleNamespace(
        source=SimpleNamespace(platform="telegram", user_id=user_id, chat_id=chat_id),
        message_id=f"knowledge-page:{callback_id}",
        text=f"knowledge show {knowledge_id} {page} {content_digest}",
    )
    # Navigation is an authenticated read through the same CLI as knowledge
    # show. It cannot invoke approve, change modes or resurrect old content.
    ok, value = await asyncio.to_thread(_run_control, event)
    if not ok or not isinstance(value, dict) or not value.get("preview"):
        await query.answer(
            text=(str(value) if not ok else "知识预览返回无效")[:190], show_alert=True
        )
        return
    preview = value["preview"]
    preview["buttons"].append(
        {"text": "返回工作台", "callback_data": "wb2:open:k", "row": 1}
    )
    await query.answer(text=f"第 {preview['page']}/{preview['page_count']} 页")
    try:
        await query.edit_message_text(
            text=str(preview["text"]),
            parse_mode="HTML",
            reply_markup=_telegram_reply_markup(list(preview["buttons"])),
        )
    except Exception:
        LOGGER.exception("Unable to display knowledge preview page")


async def _handle_workbench_callback(query: Any, data: str) -> None:
    navigation = re.fullmatch(
        r"(?:wb2:open:[amihwepkc]|(?:wb2|wi2|wd2):[A-Za-z0-9_-]+)", data
    )
    action = re.fullmatch(
        r"wka2:([rocas]):(K3-[A-Za-z0-9_-]{1,28}):([A-Za-z0-9_-]{22})", data
    )
    if data.startswith(("wka:", "wkb:", "wki:", "wkd:")):
        await query.answer(text="旧按钮已失效，请刷新 Case 详情", show_alert=True)
        return
    if len(data.encode()) > 64:
        await query.answer(text="无效工作台按钮", show_alert=True)
        return
    if navigation:
        text = "workbench-nav " + data
    elif action:
        code, case_id, token = action.groups()
        operation = {
            "r": "resolve",
            "o": "reopen",
            "c": "claim",
            "a": "delegate",
            "s": "suggest_only",
        }[code]
        text = f"case-action {operation} {case_id} {token}"
        # This cursor restores browsing position only. The authoritative CLI
        # separately validates owner identity and the exact action binding.
        markup = getattr(getattr(query, "message", None), "reply_markup", None)
        if markup is not None:
            value = markup if isinstance(markup, dict) else markup.to_dict()
            origins = [
                str(button.get("callback_data", ""))
                for row in value.get("inline_keyboard", [])
                for button in row
                if str(button.get("callback_data", "")).startswith(("wb2:", "wi2:", "wd2:"))
            ]
            if len(origins) > 1 or (
                origins
                and not re.fullmatch(
                    r"wb2:(?:open:[amihwepkc]|[A-Za-z0-9_-]+)", origins[0]
                )
            ):
                await query.answer(
                    text="返回位置无效，请刷新 Case 详情", show_alert=True
                )
                return
            if origins:
                text += " return " + origins[0]
    else:
        await query.answer(text="无效工作台按钮", show_alert=True)
        return
    message = getattr(query, "message", None)
    user_id = str(getattr(getattr(query, "from_user", None), "id", "") or "")
    chat_id = str(getattr(message, "chat_id", "") or "")
    query_id = str(getattr(query, "id", "") or "")
    if not all((user_id, chat_id, query_id)):
        await query.answer(text="工作台上下文不完整", show_alert=True)
        return
    event = SimpleNamespace(
        source=SimpleNamespace(platform="telegram", user_id=user_id, chat_id=chat_id),
        message_id=f"workbench:{query_id}",
        text=text,
    )
    ok, value = await asyncio.to_thread(_run_control, event)
    await _show_workbench_preview(query, ok, value)


async def _show_workbench_preview(query: Any, ok: bool, value: Any) -> None:
    if (
        not ok
        or not isinstance(value, dict)
        or not isinstance(value.get("preview"), dict)
    ):
        await query.answer(
            text=(str(value) if not ok else "工作台返回无效")[:190], show_alert=True
        )
        return
    preview = value["preview"]
    command = value.get("command")
    notice = {
        "resolve": "已由你标记解决",
        "reopen": "已重新打开，由你负责",
        "claim": "已切换：我来回复",
        "suggest_only": "已切换：只给我建议",
        "delegate": "已交给 AI",
    }.get(command)
    if command == "mail_preview" and value.get("operation") == "corrected":
        notice = "已纠正当前分类；历史摘要未改变"
    position = preview.get("position_label")
    if not position and "page" in preview and "page_count" in preview:
        position = f"第 {preview['page']}/{preview['page_count']} 页"
    await query.answer(text=notice or position or "已更新")
    try:
        await query.edit_message_text(
            text=str(preview["text"]),
            parse_mode="HTML",
            reply_markup=_telegram_reply_markup(list(preview["buttons"])) if preview["buttons"] else None,
        )
    except Exception:
        LOGGER.exception("Unable to display workbench view")


async def _handle_mail_callback(query: Any, data: str) -> None:
    message = getattr(query, "message", None)
    user_id = str(getattr(getattr(query, "from_user", None), "id", "") or "")
    chat_id = str(getattr(message, "chat_id", "") or "")
    prompt_id = str(getattr(message, "message_id", "") or "")
    callback_id = str(getattr(query, "id", "") or "")
    if not all((user_id, chat_id, prompt_id, callback_id)):
        await query.answer(text="邮件摘要上下文不完整", show_alert=True)
        return
    event = SimpleNamespace(
        source=SimpleNamespace(platform="telegram", user_id=user_id, chat_id=chat_id),
        message_id=f"mail-view:{callback_id}",
        text=f"mail-view {shlex.quote(prompt_id)} {shlex.quote(data)}",
    )
    ok, value = await asyncio.to_thread(_run_control, event)
    await _show_workbench_preview(query, ok, value)


async def _handle_meeting_callback(query: Any, data: str) -> None:
    message = getattr(query, "message", None)
    user_id = str(getattr(getattr(query, "from_user", None), "id", "") or "")
    chat_id = str(getattr(message, "chat_id", "") or "")
    prompt_id = str(getattr(message, "message_id", "") or "")
    callback_id = str(getattr(query, "id", "") or "")
    if not all((user_id, chat_id, prompt_id, callback_id)):
        await query.answer(text="会议审批上下文不完整", show_alert=True)
        return
    command = "remote-recovery" if data.startswith("rr:") else ("meeting-recovery" if data.startswith("mr:") else "meeting-view")
    event = SimpleNamespace(
        source=SimpleNamespace(platform="telegram", user_id=user_id, chat_id=chat_id),
        message_id=f"{command}:{callback_id}",
        text=f"{command} {shlex.quote(prompt_id)} {shlex.quote(data)}",
    )
    ok, value = await asyncio.to_thread(_run_control, event)
    if ok and isinstance(value, dict) and "preview" in value:
        await _show_workbench_preview(query, ok, value)
        return
    receipt = _format_receipt(ok, value)
    await query.answer(text=("已处理" if ok else receipt)[:190], show_alert=not ok)
    if ok:
        await query.edit_message_text(text=receipt, reply_markup=None)


async def _handle_global_callback(query: Any, data: str) -> None:
    parts = data.split(":", 2)
    actions = {
        "o": "global_observe",
        "c": "global_collaborate",
        "t": "global_auto_60",
        "a": "global_auto_request",
        "A": "global_auto_confirm",
        "p": "global_pause",
        "s": "global_stop_request",
        "S": "global_stop_confirm",
        "x": "global_cancel_confirmation",
        "i": "global_details",
        "r": "global_refresh",
        "w": "global_workbench",
    }
    if len(parts) != 3 or parts[1] not in actions or not parts[2].startswith("gcp_"):
        await query.answer(text="无效总控按钮")
        return
    message = getattr(query, "message", None)
    coordinates = {
        "user_id": str(getattr(getattr(query, "from_user", None), "id", "") or ""),
        "chat_id": str(getattr(message, "chat_id", "") or ""),
        "prompt_message_id": str(getattr(message, "message_id", "") or ""),
        "callback_query_id": str(getattr(query, "id", "") or ""),
    }
    if not all(coordinates.values()):
        await query.answer(text="总控上下文不完整", show_alert=True)
        return
    action = actions[parts[1]]
    ok, value = await asyncio.to_thread(
        _run_callback,
        **coordinates,
        action=action,
        target_id=parts[2],
        target_type="panel",
    )
    if action == "global_workbench":
        await _show_workbench_preview(query, ok, value)
        return
    if not ok or not isinstance(value, dict) or value.get("command") != "global_panel":
        await query.answer(
            text=(str(value) if not ok else "总控返回无效")[:190], show_alert=True
        )
        return
    mode_labels = {
        "observe": "观察",
        "collaborate": "协作",
        "auto_60": "自动 60 分钟",
        "auto": "自动",
        "paused": "立即暂停",
        "stopped": "完全停止",
    }
    if action == "global_refresh":
        receipt = "已刷新"
        show_alert = False
    elif action == "global_details":
        receipt = "已显示详情"
        show_alert = False
    elif action == "global_auto_request":
        receipt = "请再次确认进入自动模式"
        show_alert = True
    elif action == "global_stop_request":
        receipt = "请再次确认完全停止"
        show_alert = True
    elif action == "global_cancel_confirmation":
        receipt = "已取消"
        show_alert = True
    else:
        receipt = f"已切换：{mode_labels.get(str(value.get('mode')), '未知模式')}"
        show_alert = True
    await query.answer(text=receipt, show_alert=show_alert)
    try:
        terminal_action = action in {
            "global_observe",
            "global_collaborate",
            "global_auto_60",
            "global_auto_confirm",
            "global_pause",
            "global_stop_confirm",
        }
        if terminal_action:
            await query.edit_message_text(
                text=f"✅ {receipt}\n{value['text']}",
                reply_markup=None,
            )
        else:
            await query.edit_message_text(
                text=str(value["text"]),
                reply_markup=_telegram_reply_markup(list(value["buttons"])),
            )
        LOGGER.info(
            "Feishu global control updated: action=%s mode=%s revision=%s",
            action,
            value.get("mode"),
            value.get("revision"),
        )
    except Exception:
        LOGGER.exception("Unable to refresh Feishu global control panel")


async def _handle_k3_callback(query: Any, data: str) -> None:
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[1] not in {"a", "d", "i"}:
        await query.answer(text="无效审批按钮")
        return
    message = getattr(query, "message", None)
    user_id = str(getattr(getattr(query, "from_user", None), "id", "") or "")
    chat_id = str(getattr(message, "chat_id", "") or "")
    prompt_message_id = str(getattr(message, "message_id", "") or "")
    callback_query_id = str(getattr(query, "id", "") or "")
    if not all((user_id, chat_id, prompt_message_id, callback_query_id)):
        await query.answer(text="审批上下文不完整", show_alert=True)
        return
    action = {"a": "approve", "d": "deny", "i": "details"}[parts[1]]
    ok, value = await asyncio.to_thread(
        _run_callback,
        user_id=user_id,
        chat_id=chat_id,
        callback_query_id=callback_query_id,
        prompt_message_id=prompt_message_id,
        action=action,
        target_id=parts[2],
    )
    if action == "details" and ok and isinstance(value, dict):
        approval = value.get("approval") or {}
        detail = (
            f"Case: {approval.get('case_id', '-')}\n"
            f"类型: {value.get('gate', '-')}\n"
            f"状态: {approval.get('status', '-')}\n"
            f"失效: {approval.get('expires_at', '-')}"
        )
        await query.answer(text=detail[:190], show_alert=True)
        return
    receipt = _format_receipt(ok, value)
    await query.answer(text=("已处理" if ok else str(receipt))[:190], show_alert=not ok)
    if ok:
        try:
            await query.edit_message_text(text=receipt, reply_markup=None)
        except Exception:
            LOGGER.exception("Unable to replace resolved K3 approval prompt")


async def _handle_k3_case_callback(query: Any, data: str) -> None:
    parts = data.split(":", 2)
    actions = {
        "c": "claim",
        "s": "suggest_only",
        "a": "delegate",
        "t": "takeover",
        "p": "pause",
        "i": "details",
    }
    if len(parts) != 3 or parts[1] not in actions or not parts[2].startswith("K3-"):
        await query.answer(text="无效 Case 控制按钮")
        return
    message = getattr(query, "message", None)
    coordinates = {
        "user_id": str(getattr(getattr(query, "from_user", None), "id", "") or ""),
        "chat_id": str(getattr(message, "chat_id", "") or ""),
        "prompt_message_id": str(getattr(message, "message_id", "") or ""),
        "callback_query_id": str(getattr(query, "id", "") or ""),
    }
    if not all(coordinates.values()):
        await query.answer(text="Case 控制上下文不完整", show_alert=True)
        return
    action = actions[parts[1]]
    ok, value = await asyncio.to_thread(
        _run_callback,
        **coordinates,
        action=action,
        target_id=parts[2],
        target_type="case",
    )
    if action == "details" and ok and isinstance(value, dict):
        turn = (value.get("turns") or [{}])[0]
        await query.answer(
            text=(
                f"Case: {value.get('case_id', '-')}\n"
                f"沟通权: {turn.get('communication_owner', '-')} / {turn.get('communication_mode', '-')}\n"
                f"Turn: {turn.get('state', '-')}"
            )[:190],
            show_alert=True,
        )
        return
    receipt = _format_receipt(ok, value)
    await query.answer(text=("已处理" if ok else receipt)[:190], show_alert=not ok)
    if ok:
        try:
            await query.edit_message_text(text=receipt, reply_markup=None)
        except Exception:
            LOGGER.exception("Unable to replace resolved K3 Case prompt")


def _install_callback_handler() -> None:
    """Install a narrow, fail-closed extension on Hermes' Telegram adapter."""
    TelegramAdapter = None
    selected_module = None
    # Bundled platform plugins are lazy-loaded.  Resolve Telegram through the
    # public registry before looking up its namespaced module; otherwise this
    # user plugin is registered first and can only see the legacy source-tree
    # module, which is not the class the gateway later instantiates.
    try:
        registry_module = importlib.import_module("gateway.platform_registry")
        registry_module.platform_registry.get("telegram")
    except Exception:
        LOGGER.debug("Unable to resolve the lazy Telegram platform", exc_info=True)
    # Current Hermes loads directory plugins into its isolated
    # ``hermes_plugins`` namespace.  Older releases imported the Telegram
    # adapter through the source-tree ``plugins`` package.  Patching the latter
    # when both names exist silently succeeds but wraps a different class, so
    # commands work while every inline-button callback falls through to
    # Hermes' generic handler.  Bind the live plugin namespace first and keep
    # the old name only as a compatibility fallback.
    for module_name in (
        "hermes_plugins.telegram_platform.adapter",
        "plugins.platforms.telegram.adapter",
    ):
        try:
            module = importlib.import_module(module_name)
            candidate = module.TelegramAdapter
        except (ImportError, AttributeError):
            continue
        TelegramAdapter = candidate
        selected_module = module_name
        break
    if TelegramAdapter is None:
        LOGGER.error("Telegram adapter unavailable for K3 approval buttons")
        return
    original = TelegramAdapter._handle_callback_query
    if getattr(original, "_k3_support_wrapped", False):
        return

    async def wrapped(self: Any, update: Any, context: Any) -> None:
        query = getattr(update, "callback_query", None)
        data = str(getattr(query, "data", "") or "")
        if data.startswith("k3a:"):
            await _handle_k3_callback(query, data)
            return
        if data.startswith("k3c:"):
            await _handle_k3_case_callback(query, data)
            return
        if data.startswith("fsc:"):
            await _handle_global_callback(query, data)
            return
        if data.startswith("knp:"):
            await _handle_knowledge_callback(query, data)
            return
        if data.startswith(
            ("wb2:", "wi2:", "wd2:", "wkb:", "wki:", "wkd:", "wka:", "wka2:")
        ):
            await _handle_workbench_callback(query, data)
            return
        if data.startswith("ml:"):
            await _handle_mail_callback(query, data)
            return
        if data.startswith(("mt:", "mr:", "rr:")):
            await _handle_meeting_callback(query, data)
            return
        await original(self, update, context)

    wrapped._k3_support_wrapped = True  # type: ignore[attr-defined]
    TelegramAdapter._handle_callback_query = wrapped
    LOGGER.info("Installed K3 support callbacks on %s", selected_module)


def _telegram_token() -> str:
    try:
        from hermes_cli.gateway import get_env_value  # type: ignore[import-not-found]

        token = get_env_value("TELEGRAM_BOT_TOKEN") or ""
    except (ImportError, OSError, RuntimeError):
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token.strip():
        raise PluginConfigurationError("Telegram bot token 未配置")
    return token.strip()


def _setup_telegram_cli(parser: Any) -> None:
    sub = parser.add_subparsers(dest="k3_support_telegram_command", required=True)
    send = sub.add_parser("send-buttons")
    send.add_argument("--to", required=True)
    send.add_argument("--text", required=True)
    send.add_argument("--buttons-json", required=True)
    send.add_argument("--parse-mode", choices=("HTML",))


def _telegram_cli(args: Any) -> None:
    if args.k3_support_telegram_command != "send-buttons":
        raise PluginConfigurationError("unsupported Telegram helper command")
    buttons = json.loads(args.buttons_json)
    if not isinstance(buttons, list) or not 1 <= len(buttons) <= 8:
        raise PluginConfigurationError("buttons 必须是 1..8 项数组")
    message_id = _send_button_message(
        str(args.to), args.text, buttons, getattr(args, "parse_mode", None)
    )
    rendered_id: str | int = int(message_id) if message_id.isdigit() else message_id
    print(json.dumps({"ok": True, "message_id": rendered_id}, ensure_ascii=False))


async def _send_receipt(adapter: Any, chat_id: str, message_id: str, text: str) -> None:
    result = await adapter.send(chat_id, text, reply_to=message_id)
    if getattr(result, "success", True) is False:
        LOGGER.error("K3 support control receipt delivery failed")


async def _execute_and_reply(adapter: Any, event: Any) -> None:
    try:
        ok, value = await asyncio.to_thread(_run_control, event)
        if _platform_name(getattr(event, "source", None)) == "feishu":
            source = event.source
            if ok and isinstance(value, dict):
                from .feishu_cards import send
                if await send(adapter, event, value, run_control=_run_control):
                    return
            if (
                ok
                and isinstance(value, dict)
                and value.get("command")
                in {"approval_detail", "feishu_mode", "project_bug_approval"}
            ):
                receipt = str(value["text"]) + "\n\n" + "\n".join(value["commands"])
            else:
                receipt = _format_receipt(ok, value)
                if ok and isinstance(value, dict) and isinstance(value.get("preview"), dict):
                    import html
                    receipt = html.unescape(re.sub(r"<[^>]+>", "", receipt))
                    navigation = ["workbench-nav " + button["callback_data"]
                                  for button in value["preview"].get("buttons", [])
                                  if isinstance(button.get("callback_data"), str) and button["callback_data"].startswith(("wb2:", "wi2:", "wd2:"))]
                    if navigation:
                        receipt += "\n\n导航命令：\n" + "\n".join(navigation)
            await _send_receipt(adapter, str(source.chat_id), str(getattr(event, "control_reply_to", None) or getattr(event, "message_id", "") or ""), receipt)
            return
        if ok and isinstance(value, dict) and value.get('command') == 'meeting_recovery':
            source = getattr(event, 'source', None)
            chat_id = str(getattr(source, 'chat_id', '') or '')
            target, preview = value.get('display_target') or {}, value.get('preview') or {}
            if (target.get('kind') != 'edit_original' or str(target.get('chat_id')) != chat_id
                    or not target.get('message_id') or not preview.get('text')
                    or not isinstance(preview.get('buttons'), list)):
                raise PluginConfigurationError('恢复结果缺少原审批卡绑定，未另发可操作卡片')
            try:
                await asyncio.to_thread(_send_button_message, chat_id, str(preview['text']),
                                        preview['buttons'], 'HTML', edit_message_id=str(target['message_id']))
                notice = '核对结果已更新在原会议审批卡；没有再次创建或邀请。'
            except PluginConfigurationError:
                notice = '本地恢复结果已记录，但原会议审批卡更新失败。请回原卡刷新状态；不要重试创建。'
            await _send_receipt(adapter, chat_id, str(getattr(event, 'message_id', '') or ''), notice)
            return
        if (
            ok
            and isinstance(value, dict)
            and value.get("command")
            in {"knowledge", "workbench", "mail_preview", "meeting_preview"}
            and isinstance(value.get("preview"), dict)
        ):
            source = getattr(event, "source", None)
            preview = value["preview"]
            await asyncio.to_thread(
                _send_button_message,
                str(getattr(source, "chat_id", "") or ""),
                str(preview["text"]),
                list(preview["buttons"]),
                "HTML",
            )
            return
        if ok and isinstance(value, dict) and value.get("command") == "global_panel":
            source = getattr(event, "source", None)
            user_id = str(getattr(source, "user_id", "") or "")
            chat_id = str(getattr(source, "chat_id", "") or "")
            command_message_id = str(
                getattr(event, "message_id", None)
                or getattr(source, "message_id", None)
                or ""
            )
            prompt_message_id = await asyncio.to_thread(
                _send_button_message,
                chat_id,
                str(value["text"]),
                list(value["buttons"]),
            )
            bound, bind_value = await asyncio.to_thread(
                _run_panel_bind,
                user_id=user_id,
                chat_id=chat_id,
                command_message_id=command_message_id,
                prompt_message_id=prompt_message_id,
                panel_id=str(value["panel_id"]),
            )
            if not bound:
                await _send_receipt(
                    adapter,
                    chat_id,
                    command_message_id,
                    f"❌ 总控面板未绑定：{bind_value}",
                )
            return
        receipt = _format_receipt(ok, value)
    except Exception:
        LOGGER.exception("K3 support control adapter failed closed")
        receipt = "❌ 控制命令未执行：控制适配器内部错误"
        if _platform_name(getattr(event, "source", None)) == "feishu":
            receipt = "控制回执未确认，请重新打开工作台核对当前状态。"
    source = getattr(event, "source", None)
    chat_id = str(getattr(source, "chat_id", ""))
    message_id = str(
        getattr(event, "control_reply_to", None) or getattr(event, "message_id", None) or getattr(source, "message_id", None) or ""
    )
    await _send_receipt(adapter, chat_id, message_id, receipt)


def _schedule_control(gateway: Any, event: Any) -> bool:
    source = getattr(event, "source", None)
    adapters = getattr(gateway, "adapters", {})
    adapter = adapters.get(getattr(source, "platform", None))
    if adapter is None:
        LOGGER.error("Control channel adapter unavailable for K3 support control receipt")
        return False
    try:
        task = asyncio.get_running_loop().create_task(
            _execute_and_reply(adapter, event)
        )
    except RuntimeError:
        LOGGER.exception("No running event loop for K3 support control receipt")
        return False

    def _observe(completed: asyncio.Task[Any]) -> None:
        try:
            completed.result()
        except Exception:
            LOGGER.exception("K3 support control receipt delivery raised")

    task.add_done_callback(_observe)
    return True


def pre_gateway_dispatch(event: Any, gateway: Any, **_: Any) -> dict[str, str] | None:
    """Consume exact Telegram/Feishu control messages before Hermes invokes an LLM."""

    text = str(getattr(event, "text", "") or "").strip()
    source = getattr(event, "source", None)
    if _platform_name(source) == "feishu":
        from .feishu_cards import callback_event
        owned, callback = callback_event(event)
        if owned:
            if callback is not None and _is_control_message(callback.text):
                _schedule_control(gateway, callback)
            else:
                LOGGER.warning("Rejected malformed Feishu control callback")
            return {"action": "skip", "reason": "k3-support-control"}
    if _platform_name(source) not in {"telegram", "feishu"} or not _is_control_message(text):
        return None
    # The hook is synchronous, but the gateway loop must not be blocked by the
    # bounded subprocess. Schedule it in a worker thread and consume the
    # control-shaped message immediately so it can never reach an LLM.
    _schedule_control(gateway, event)
    return {"action": "skip", "reason": "k3-support-control"}


def _feishu_slash_fallback(raw_args: str) -> str:
    """Fail closed if a surface dispatches the registered command without the hook.

    Telegram normally reaches ``pre_gateway_dispatch`` first because opening a
    panel needs stable sender/chat/message IDs.  Registering the command as well
    makes it discoverable in ``/commands`` and Telegram's command menu.  This
    fallback must not guess those IDs on a surface that bypasses gateway hooks.
    """

    if raw_args.strip():
        return "用法：/feishu"
    return "请在已配置的 Telegram 私聊中发送 /feishu 打开飞书自动办公总控。"


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    if hasattr(ctx, "register_command"):
        ctx.register_command(
            "feishu",
            handler=_feishu_slash_fallback,
            description="打开飞书自动办公总控",
        )
    if hasattr(ctx, "register_cli_command"):
        ctx.register_cli_command(
            "k3-support-telegram",
            "Send deterministic K3 approval buttons",
            _setup_telegram_cli,
            _telegram_cli,
        )
    _install_callback_handler()
