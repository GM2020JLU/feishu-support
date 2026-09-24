"""Authenticated loopback control surface; never expose this server publicly."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import shlex
import sqlite3
import stat
import threading
import time
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from importlib import resources
from pathlib import Path

from .approvals import ApprovalError, approval_binding
from .bounded_http import BoundedHTTPServer
from .calendar import CalendarError
from .case_detail import case_detail
from .config import load_config
from .control import ControlMessage, _decide_and_continue, execute_control
from .db import connect, migrate
from .executors import ExecutorError
from .meeting_recovery import MeetingRecoveryError
from .runtime_control import (
    bind_global_panel,
    execute_global_callback,
    issue_global_panel,
)
from .store import ConflictError, NotFoundError
from .workbench import workbench_page, workbench_snapshot

ACTIONS = {
    "o": "global_observe",
    "c": "global_collaborate",
    "t": "global_auto_60",
    "a": "global_auto_request",
    "A": "global_auto_confirm",
    "p": "global_pause",
    "s": "global_stop_request",
    "S": "global_stop_confirm",
    "x": "global_cancel_confirmation",
}


class GuiError(ValueError):
    pass


class Console:
    def __init__(self, config, login_key):
        self.config = config
        self.key_hash = hashlib.sha256(login_key.encode()).digest()
        self.sessions = {}
        self.session_lock = threading.RLock()
        self.failures = []
        from .gui_inference import PreviewTasks
        self.previews = PreviewTasks(config, require_session_registration=True)

    def login(self, key):
        with self.session_lock:
            return self._login_locked(key)

    def _login_locked(self, key):
        now = time.monotonic()
        self.failures = [stamp for stamp in self.failures if stamp > now - 60]
        if len(self.failures) >= 10:
            raise GuiError("登录尝试过多，请一分钟后重试")
        if not isinstance(key, str) or not hmac.compare_digest(
            hashlib.sha256(key.encode()).digest(), self.key_hash
        ):
            self.failures.append(now)
            raise GuiError("登录密钥不正确")
        for expired in [token for token, value in self.sessions.items() if value['expires'] <= now]:
            self._logout_locked(expired)
        if len(self.sessions) >= 8:
            raise GuiError("会话数量已达上限，请退出其他会话")
        token = secrets.token_urlsafe(32)
        self.sessions[token] = {
            "expires": now + 8 * 3600,
            "csrf": secrets.token_urlsafe(32),
            "chat_id": "gui:" + secrets.token_hex(16),
            "panel": None,
            "actions": {},
            "case_buttons": {},
            "approval_buttons": {},
            "_views": {},
            "_view_revision": 0,
        }
        self.previews.activate_session(token, expires_at=self.sessions[token]['expires'])
        return token

    def session(self, token):
        with self.session_lock:
            return self._session_locked(token)

    def _session_locked(self, token):
        value = self.sessions.get(token)
        if not value or value["expires"] <= time.monotonic():
            self._logout_locked(token)
            raise PermissionError("请先登录控制台")
        return value

    def _logout_locked(self, token):
        self.sessions.pop(token, None)
        self.previews.revoke_session(token)

    def logout(self, token):
        with self.session_lock:
            self._logout_locked(token)

    def _assert_live_session(self, session):
        if session['expires'] <= time.monotonic() or not any(value is session for value in self.sessions.values()):
            raise PermissionError('会话已注销或过期，操作未受理')

    def _begin_view(self, session, kind, identifier):
        if not isinstance(identifier, str) or len(identifier) > 256:
            raise GuiError('页面标识不合法')
        with self.session_lock:
            self._assert_live_session(session)
            session['_view_revision'] += 1
            generation = session['_view_revision']
            key = (kind, identifier)
            session['_views'][key] = generation
            if len(session['_views']) > 256:
                session['_views'].pop(next(iter(session['_views'])))
            return key, generation

    def _check_view(self, session, view):
        self._assert_live_session(session)
        if session['_views'].get(view[0]) != view[1]:
            raise GuiError('页面已被较新的请求替换，请重新读取')

    def data(self, conn):
        from .broker_launch_status import snapshot as launch_status
        from .feature_settings import effective
        from .notification_snooze import status as notification_status

        state = conn.execute(
            "SELECT mode,revision,auto_expires_at FROM global_control_state WHERE scope='feishu_support'"
        ).fetchone()
        return {
            "mode": dict(state) if state else {"mode": "observe", "revision": 0},
            "static_mode": self.config.mode,
            "broker_launches": launch_status(conn),
            "workbench": workbench_snapshot(conn, config=self.config, limit=30),
            "features": effective(self.config),
            "notification_snooze": notification_status(conn,config=self.config),
            "services": [
                dict(row)
                for row in conn.execute(
                    "SELECT component,status,heartbeat_at FROM service_state ORDER BY component"
                )
            ],
            "knowledge": [
                dict(row)
                for row in conn.execute(
                    "SELECT knowledge_id,title,status,project,module FROM knowledge_entries ORDER BY updated_at DESC LIMIT 50"
                )
            ],
            "work_hours": self.config.work_hours,
        }

    def panel(self, conn, session):
        view = self._begin_view(session, 'panel', '')
        operator = self.config.control_operator_id
        if not operator:
            raise GuiError("尚未配置控制者身份")
        command_id = "gui:" + secrets.token_hex(16)
        panel = issue_global_panel(
            conn,
            self.config,
            operator_user_id=operator,
            chat_id=session["chat_id"],
            command_message_id=command_id,
            control_channel="gui",
        )
        bind_global_panel(
            conn,
            panel_id=panel["panel_id"],
            operator_user_id=operator,
            chat_id=session["chat_id"],
            command_message_id=command_id,
            prompt_message_id=command_id,
        )
        with self.session_lock:
            self._check_view(session, view)
            session["panel"] = {
                "id": panel["panel_id"],
                "prompt": command_id,
                "buttons": [b["callback_data"] for b in panel["buttons"]],
            }
        return panel

    def detail(self, conn, payload, session=None):
        view = self._begin_view(session, 'case', payload.get('case_id', '')) if session is not None else None
        page = payload.get("page", 1)
        fingerprint = payload.get("content_digest")
        if type(page) is not int or not 1 <= page <= 9999:
            raise GuiError("详情页码不合法")
        if page > 1 and not fingerprint:
            raise GuiError("翻页需要内容版本，请重新打开详情")
        if fingerprint is not None and (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 16
            or any(char not in "0123456789abcdef" for char in fingerprint)
        ):
            raise GuiError("详情版本不合法")
        result = case_detail(
            conn,
            case_id=payload.get("case_id", ""),
            page=page,
            expected_digest=fingerprint,
            origin_cursor=payload.get("origin_cursor"),
        )
        preview = result["preview"]
        actions = []
        if session is not None:
            with self.session_lock:
                self._check_view(session, view)
                now = time.monotonic()
                session["case_buttons"] = {
                    key: value
                    for key, value in session["case_buttons"].items()
                    if value["expires"] > now and value["case_id"] != result["case_id"]
                }
                # Bound memory while allowing multiple Case tabs in this session.
                if len(session["case_buttons"]) > 120:
                    session["case_buttons"].clear()
                for button in preview["buttons"]:
                    parts = button["callback_data"].split(":")
                    if (
                        len(parts) != 4
                        or parts[0] != "wka2"
                        or parts[1] not in {"c", "s", "a", "r", "o"}
                    ):
                        continue
                    token = secrets.token_urlsafe(24)
                    session["case_buttons"][token] = {
                        "case_id": result["case_id"],
                        "expires": now + 300,
                        "action": {
                            "c": "claim",
                            "s": "suggest_only",
                            "a": "delegate",
                            "r": "resolve",
                            "o": "reopen",
                        }[parts[1]],
                        "binding": parts[3],
                        "origin": result["origin_cursor"],
                    }
                    actions.append({"token": token, "label": button["text"]})
        return {
            "case_id": result["case_id"],
            "sent_knowledge": [dict(row) for row in conn.execute(
                "SELECT use_id,created_at FROM knowledge_uses WHERE case_id=? ORDER BY created_at DESC,use_id DESC LIMIT 10",
                (result["case_id"],))],
            "origin_cursor": result["origin_cursor"],
            "text": preview["plain_text"],
            "page": preview["page"],
            "page_count": preview["page_count"],
            "content_digest": preview["content_digest"],
            "read_only": True,
            "actions": actions,
            "linked_approvals": [
                dict(row)
                for row in conn.execute(
                    "SELECT approval_id,approval_type,status FROM approvals WHERE case_id=? ORDER BY created_at DESC,approval_id DESC LIMIT 20",
                    (result["case_id"],),
                )
            ],
        }

    def case_action(self, conn, session, payload):
        if (
            not self.config.control_operator_id
            or not self.config.web_control_chat_id
        ):
            raise GuiError("尚未配置完整控制者身份")
        request_id, token = payload.get("request_id"), payload.get("token")
        if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
            raise GuiError("操作请求编号不合法")
        if not isinstance(token, str):
            raise GuiError("操作令牌不合法")
        signature = "case:" + token
        with self.session_lock:
            self._assert_live_session(session)
            previous = session["actions"].get(request_id)
            if previous:
                if previous[0] != signature:
                    raise GuiError("操作请求编号已用于其他动作")
                if "error" in previous[1]:
                    raise GuiError(previous[1]["error"])
                return previous[1]
            if len(session["actions"]) >= 256:
                raise GuiError("本会话操作数量已达上限，请重新登录")
            target = session["case_buttons"].get(token)
            if target is None or target["expires"] <= time.monotonic():
                raise GuiError("任务操作已失效，请重新读取详情")
            if target["action"] in {"resolve", "reopen"} and not target.get("confirmed"):
                confirm_token = secrets.token_urlsafe(24)
                session["case_buttons"][confirm_token] = {**target, "confirmed": True}
                del session["case_buttons"][token]
                response = {
                    "requires_confirmation": True,
                    "confirmation": {
                        "token": confirm_token,
                        "label": "确认标记解决"
                        if target["action"] == "resolve"
                        else "确认重新打开",
                    },
                    "message": (
                        "确认由你标记问题已解决？将撤销本轮任务、审批和待发送回复；设备占用仍需安全收尾，已在途操作不保证撤回。"
                        if target["action"] == "resolve"
                        else "确认重新打开并由你负责？会开启新轮次，但不会恢复旧任务或旧审批，也不会自动交回 AI。"
                    ),
                }
                session["actions"][request_id] = (signature, response)
                return response
            # Reserve before executing: an exception after a committed transition must
            # never cause a retry to execute a second transition or continuation.
            uncertain = {"error": "操作结果未确认，请重新读取详情核对，不要重复提交"}
            session["actions"][request_id] = (signature, uncertain)
        result = execute_control(
            conn,
            self.config,
            ControlMessage(
                user_id=self.config.control_operator_id,
                chat_id=self.config.web_control_chat_id,
                message_id=session["chat_id"] + ":" + request_id,
                text=shlex.join(
                    [
                        "case-action",
                        target["action"],
                        target["case_id"],
                        target["binding"],
                        "return",
                        target["origin"],
                    ]
                ),
            ),
            control_channel="gui",
        )
        response = {
            "ok": True,
            "case_id": target["case_id"],
            "action": target["action"],
            "communication_changed": True,
            "continuation": result.get("continuation"),
            "state": result.get("state"),
            "message": (
                "已由你标记解决；本轮任务和审批已撤销，设备及在途操作仍需确认收尾。"
                if target["action"] == "resolve"
                else "已重新打开，由你负责；旧任务和审批没有恢复。"
                if target["action"] == "reopen"
                else "沟通控制已更新；后台测试未因此停止，已在途发送也不保证撤回。"
            ),
        }
        with self.session_lock:
            session["actions"][request_id] = (signature, response)
        return response

    def approval_detail(self, conn, session, payload):
        approval_id = payload.get("approval_id")
        view = self._begin_view(session, 'approval', approval_id)
        if (
            not isinstance(approval_id, str)
            or not approval_id.startswith("apr_")
            or len(approval_id) > 64
        ):
            raise GuiError("审批编号不合法")
        conn.execute("SAVEPOINT gui_approval_read")
        try:
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()
            if row is None:
                raise GuiError("审批不存在")
            binding = approval_binding(conn, approval_id)
            action = json.loads(row["requested_action_json"])
        finally:
            conn.execute("RELEASE gui_approval_read")
        buttons = []
        supported = row["approval_type"] in {"board1_lease", "wip_push"}
        meeting = None
        if row["approval_type"] == "meeting_create":
            from .gui_meeting_preview import meeting_view

            meeting = meeting_view(conn, self.config, approval_id)
            supported = meeting is not None and meeting["confirm_label"] is not None
        with self.session_lock:
            self._check_view(session, view)
            now = time.monotonic()
            session["approval_buttons"] = {
                key: value
                for key, value in session["approval_buttons"].items()
                if value["expires"] > now and value["approval_id"] != approval_id
            }
            if len(session["approval_buttons"]) > 120:
                session["approval_buttons"].clear()
            if supported and row["status"] == "requested":
                for approve in (True, False):
                    token = secrets.token_urlsafe(24)
                    session["approval_buttons"][token] = {
                        "approval_id": approval_id,
                        "binding": binding,
                        "digest": row["action_digest"],
                        "approve": approve,
                        "expires": now + 300,
                    }
                    buttons.append(
                        {
                            "token": token,
                            "label": (meeting["confirm_label"] if approve else "不创建")
                            if meeting
                            else ("同意" if approve else "不同意"),
                        }
                    )
        return {
            "approval_id": approval_id,
            "case_id": row["case_id"],
            "text": meeting["text"]
            if meeting
            else (
                f"审批：{row['approval_type']}\n状态：{row['status']}\n有效期：{row['expires_at']}\n\n完整申请内容：\n"
                + json.dumps(action, ensure_ascii=False, indent=2)
            ),
            "actions": buttons,
            "note": "会议由后台处理；这里展示已存记录，未知结果不会自动重试。"
            if meeting
            else (
                "批准仅覆盖以上具体申请；执行仍受运行模式与安全校验限制。"
                if supported
                else "此类型需使用原有完整审批预览；GUI 暂不提供批准按钮。"
            ),
        }

    def approval_action(self, conn, session, payload):
        token, request_id = payload.get("token"), payload.get("request_id")
        if (
            not isinstance(token, str)
            or not isinstance(request_id, str)
            or str(uuid.UUID(request_id)) != request_id
        ):
            raise GuiError("审批请求不合法")
        signature = "approval:" + token
        with self.session_lock:
            self._assert_live_session(session)
            previous = session["actions"].get(request_id)
            if previous:
                if previous[0] != signature or "error" in previous[1]:
                    raise GuiError("审批请求已处理或结果未确认，请重新读取核对")
                return previous[1]
            if len(session["actions"]) >= 256:
                raise GuiError("本会话操作数量已达上限，请重新登录")
            target = session["approval_buttons"].get(token)
            if target is None or target["expires"] <= time.monotonic():
                raise GuiError("审批按钮已失效，请重新读取")
            if (
                not self.config.control_operator_id
                or not self.config.web_control_chat_id
            ):
                raise GuiError("尚未配置完整控制者身份")
            session["actions"][request_id] = (signature, {"error": "审批结果未确认"})
        result = _decide_and_continue(
            conn,
            self.config,
            ControlMessage(
                self.config.control_operator_id,
                self.config.web_control_chat_id,
                session["chat_id"] + ":" + request_id,
                ("approve " if target["approve"] else "deny ")
                + target["approval_id"]
                + " "
                + target["digest"],
            ),
            approval_id=target["approval_id"],
            approve=target["approve"],
            expected_digest=target["digest"],
            expected_binding=target["binding"],
            control_channel="gui",
        )
        response = {
            "ok": True,
            "status": result["approval"]["status"],
            "message": "审批决定已记录；不代表测试、推送或会议创建已经完成。",
        }
        with self.session_lock:
            session["actions"][request_id] = (signature, response)
        return response

    def action(self, conn, session, callback, request_id):
        if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
            raise GuiError("操作请求编号不合法")
        with self.session_lock:
            self._assert_live_session(session)
            previous = session["actions"].get(request_id)
            if previous:
                if previous[0] != callback:
                    raise GuiError("操作请求编号已用于其他动作")
                if 'error' in previous[1]:
                    raise GuiError(previous[1]['error'])
                return previous[1]
            if len(session["actions"]) >= 256:
                raise GuiError("本会话操作数量已达上限，请重新登录")
            panel = session["panel"]
            if not panel or callback not in panel["buttons"]:
                raise GuiError("操作已失效，请刷新控制面板")
            if panel.get('inflight'):
                raise GuiError('此面板已有操作在途或结果未确认，请刷新状态核对')
            parts = callback.split(":")
            if (
                len(parts) != 3
                or parts[0] != "fsc"
                or parts[1] not in ACTIONS
                or parts[2] != panel["id"]
            ):
                raise GuiError("不支持的控制操作")
            session["actions"][request_id] = (callback, {"error": "操作结果未确认，请刷新状态核对"})
            panel['inflight'] = request_id
        result = execute_global_callback(
            conn,
            self.config,
            action=ACTIONS[parts[1]],
            panel_id=panel["id"],
            operator_user_id=self.config.control_operator_id,
            chat_id=session["chat_id"],
            callback_query_id=session["chat_id"] + ":" + request_id,
            prompt_message_id=panel["prompt"],
        )
        with self.session_lock:
            if session['panel'] is panel:
                panel["buttons"] = [button["callback_data"] for button in result["buttons"]]
                panel.pop('inflight', None)
            session["actions"][request_id] = (callback, result)
        return result


def make_server(config, login_key, port=8765, *, max_workers=8):
    app = Console(config, login_key)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Never log keys, cookies or request bodies.

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def reply(self, status, value, *, mime="application/json", cookie=None):
            body = (
                json.dumps(value, ensure_ascii=False).encode()
                if mime == "application/json"
                else value
            )
            self.send_response(status)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def handle_request(self, write=False):
            expected_host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != expected_host:
                return self.reply(403, {"error": "拒绝非本机入口"})
            try:
                payload = {}
                if write:
                    if self.headers.get("Origin") != "http://" + expected_host:
                        raise PermissionError("跨站请求被拒绝")
                    if self.headers.get(
                        "Content-Type"
                    ) != "application/json" or self.headers.get("Transfer-Encoding"):
                        raise GuiError("仅接受 JSON 请求")
                    size = int(self.headers.get("Content-Length", "0"))
                    body_limit = (2_500_000 if self.path in {"/api/attachment-evidence-preview", "/api/attachment-draft", "/api/attachment-draft-save"}
                                  else 600_000 if self.path == "/api/knowledge-import-preview" else 16384)
                    if not 0 < size <= body_limit:
                        raise GuiError("请求大小不合法")
                    payload = json.loads(self.rfile.read(size))
                    if not isinstance(payload, dict):
                        raise GuiError("请求必须是对象")
                if not write and self.path in {"/", "/app.js", "/styles.css"}:
                    name = "index.html" if self.path == "/" else self.path[1:]
                    content = (
                        resources.files("k3_support")
                        .joinpath("gui_assets", name)
                        .read_bytes()
                    )
                    return self.reply(
                        200,
                        content,
                        mime={
                            "index.html": "text/html",
                            "app.js": "text/javascript",
                            "styles.css": "text/css",
                        }[name],
                    )
                if write and self.path == "/api/login":
                    token = app.login(payload.get("key"))
                    return self.reply(
                        200,
                        {"csrf": app.session(token)["csrf"]},
                        cookie=f"feishu_console={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800",
                    )
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                token = (
                    cookies["feishu_console"].value
                    if "feishu_console" in cookies
                    else ""
                )
                session = app.session(token)
                if write and not hmac.compare_digest(
                    self.headers.get("X-CSRF-Token", ""), session["csrf"]
                ):
                    raise PermissionError("请求校验失效，请重新登录")
                if self.path == "/api/session" and not write:
                    return self.reply(200, {"csrf": session["csrf"]})
                if self.path == "/api/logout" and write:
                    app.logout(token)
                    return self.reply(
                        200,
                        {"ok": True},
                        cookie="feishu_console=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/",
                    )
                conn = connect(config.database_path)
                try:
                    if self.path == "/api/status" and not write:
                        result = app.data(conn)
                    elif self.path == "/api/requester-profile-correct" and write:
                        from .profile_actions import apply

                        result = apply(conn, requester_id=payload.get("requester_id"), content_digest=payload.get("content_digest"), relationship=payload.get("relationship"), function_role=payload.get("function_role"), reason=payload.get("reason"), request_id=payload.get("request_id"), actor_id=config.control_operator_id, reset_auto=payload.get("reset_auto", False))
                    elif self.path == "/api/requester-profiles" and write:
                        from .profile_inventory import page

                        result = page(conn, query=payload.get("query", ""), after_id=payload.get("after_id", ""))
                    elif self.path == "/api/model-preview-start" and write:
                        result = app.previews.submit(token, payload)
                    elif self.path in {"/api/body-retention-policy", "/api/draft-retention-policy"} and write:
                        from .retention_settings import snapshot
                        result = snapshot(conn, config, scope='draft' if self.path.startswith('/api/draft-') else 'body')
                    elif self.path in {"/api/body-retention-policy-preview", "/api/draft-retention-policy-preview"} and write:
                        from .retention_settings import preview
                        if 'days' not in payload:
                            raise ValueError('必须明确选择关闭或保留天数')
                        result = preview(conn, config, days=payload.get('days'),
                            expected_revision=payload.get('expected_revision'), session_id=session['chat_id'],
                            scope='draft' if self.path.startswith('/api/draft-') else 'body')
                    elif self.path in {"/api/body-retention-policy-apply", "/api/draft-retention-policy-apply"} and write:
                        from .retention_settings import apply
                        if payload.get('confirm_policy_change') is not True:
                            raise ValueError('需要明确确认正文保留期变更')
                        result = apply(conn, config, draft_id=payload.get('draft_id'),
                            session_id=session['chat_id'], actor_id=config.control_operator_id,
                            scope='draft' if self.path.startswith('/api/draft-') else 'body')
                    elif self.path == "/api/model-preview-status" and write:
                        result = app.previews.status(token, payload.get('request_id'))
                    elif self.path == "/api/policy-comparison" and write:
                        from .policy_simulation import compare

                        result = compare(payload, minimum_confidence=float(config.raw["routing"]["minimum_route_confidence"]))
                    elif self.path == "/api/policy-simulation" and write:
                        from .policy_simulation import simulate

                        result = simulate(payload, minimum_confidence=float(config.raw["routing"]["minimum_route_confidence"]))
                    elif self.path == "/api/attachment-draft-save" and write:
                        from .knowledge_authoring import save_attachment

                        result = save_attachment(conn, fields=payload.get("fields"), expected_digest=payload.get("expected_digest"), actor_id=config.control_operator_id)
                    elif self.path in tuple('/api/retention-purge-' + action for action in
                            ('preview', 'prepare', 'execute', 'inspect', 'cancel', 'reconcile')) and write:
                        from . import retention_purge as purge

                        action = self.path.removeprefix('/api/retention-purge-')
                        actor = config.control_operator_id
                        if action == 'preview':
                            result = purge.preview(conn, config, attempt_id=payload.get('attempt_id'), days=payload.get('days'))
                        elif action == 'prepare':
                            result = purge.prepare(conn, config, attempt_id=payload.get('attempt_id'), days=payload.get('days'),
                                binding_digest=payload.get('binding_digest'), request_id=payload.get('request_id'),
                                actor_id=actor, confirm_permanent_delete=payload.get('confirm_permanent_delete'))
                        elif action == 'cancel':
                            result = purge.cancel(conn, request_id=payload.get('request_id'), actor_id=actor)
                        elif action == 'reconcile':
                            result = purge.reconcile(conn, config, request_id=payload.get('request_id'), actor_id=actor,
                                decision=payload.get('decision'), observation_digest=payload.get('observation_digest'),
                                confirm=payload.get('confirm_observation'))
                        else:
                            operation = purge.execute if action == 'execute' else purge.inspect
                            result = operation(conn, config, request_id=payload.get('request_id'), actor_id=actor)
                    elif self.path == "/api/draft-retention-preview" and write:
                        from .draft_retention import preview

                        result = preview(conn, candidate_id=payload.get('candidate_id'), days=payload.get('days'))
                    elif self.path == "/api/draft-retention-clear" and write:
                        from .draft_retention import clear

                        if payload.get('confirm_logical_delete') is not True:
                            raise ValueError('请明确确认清理草稿；备份和原文件不会被删除')
                        result = clear(conn, candidate_id=payload.get('candidate_id'), days=payload.get('days'),
                                       expected_digest=payload.get('preview_digest'), actor_id=config.control_operator_id)
                    elif self.path == "/api/knowledge-authoring-list" and write:
                        from .knowledge_authoring import page

                        result = page(conn, after_id=payload.get("after_id", ""))
                    elif self.path == "/api/knowledge-authoring-detail" and write:
                        from .knowledge_authoring import detail

                        result = detail(conn, candidate_id=payload.get("candidate_id"))
                    elif self.path == "/api/attachment-draft" and write:
                        from .docling_draft import build_draft

                        result = build_draft(evidence=payload.get("evidence"), title=payload.get("title"),
                            question=payload.get("question"), answer=payload.get("answer"),
                            references=payload.get("references"), risk_class=payload.get("risk_class"),
                            rollback=payload.get("rollback", ""), authored_scope=payload.get("authored_scope"))
                    elif self.path == "/api/attachment-evidence-preview" and write:
                        from .docling_review import preview

                        result = preview(payload.get("evidence"), page=payload.get("page", 1))
                    elif self.path == "/api/knowledge-import-preview" and write:
                        from .knowledge_import_settings import preview

                        result = preview(conn, bundle=payload.get("bundle"), session_id=session["chat_id"])
                    elif self.path == "/api/knowledge-import-apply" and write:
                        from .knowledge_import_settings import apply

                        result = apply(conn, draft_id=payload.get("draft_id"), session_id=session["chat_id"], actor_id=config.control_operator_id)
                    elif self.path == "/api/work-hours" and write:
                        from .work_hours_settings import history, snapshot

                        result = snapshot(conn, config, allow_mismatch=True)
                        result["history"] = history(conn)
                    elif self.path == "/api/work-hours-preview" and write:
                        from .work_hours_settings import preview

                        result = preview(conn, config, values=payload.get("values"), expected_revision=payload.get("expected_revision"), session_id=session["chat_id"], rollback_revision=payload.get("rollback_revision"), migrate=payload.get("migrate", False))
                    elif self.path == "/api/work-hours-apply" and write:
                        from .work_hours_settings import apply

                        result = apply(conn, config, draft_id=payload.get("draft_id"), session_id=session["chat_id"], actor_id=config.control_operator_id)
                    elif self.path == "/api/audit" and write:
                        from .audit_inventory import page

                        result = page(conn, kind=payload.get("kind", "all"), cursor=payload.get("cursor"))
                    elif self.path == "/api/execution-stop-preview" and write:
                        from .execution_stop import preview

                        result = preview(conn, job_id=payload.get("job_id"))
                    elif self.path == "/api/execution-stop" and write:
                        from .execution_stop import apply

                        result = apply(conn, job_id=payload.get("job_id"), binding_digest=payload.get("binding_digest"),
                                       request_id=payload.get("request_id"), actor_id=config.control_operator_id)
                    elif self.path == "/api/retention-recovery-check" and write:
                        from .retention_recovery import check_recovery

                        result = check_recovery(conn, config, request_id=payload.get("request_id"), actor_id=config.control_operator_id)
                    elif self.path == "/api/retention-recovery-preview" and write:
                        from .retention_recovery import recovery_preview

                        result = recovery_preview(conn, payload.get("attempt_id"))
                    elif self.path == "/api/retention-recovery-apply" and write:
                        from .operations import OperationsError
                        from .retention_recovery import apply_recovery

                        if not isinstance(payload.get("binding_digest"), str) or len(payload["binding_digest"]) != 64:
                            raise ValueError("请先预览并确认恢复记录")
                        try:
                            result = apply_recovery(conn, config, attempt_id=payload.get("attempt_id"), binding_digest=payload["binding_digest"],
                                                    request_id=payload.get("request_id"), actor_id=config.control_operator_id)
                        except OperationsError as exc:
                            raise ValueError("恢复未确认；请重新核对记录，必要时由管理员检查文件") from exc
                    elif self.path == '/api/replay-archive-capture' and write:
                        from .replay_archive_control import capture_current

                        if set(payload) != {'name', 'event', 'proposal', 'confirmed', 'writers_quiesced'}:
                            raise ValueError('归档只接受名称、事件、路由提案和两项确认')
                        try:
                            result = capture_current(conn, config, **payload)
                        except (ValueError, OSError, TimeoutError, RuntimeError):
                            raise ValueError('归档未确认完成；请先完全停止并暂停外部写入，再核对归档目录。不要自动重试') from None
                    elif self.path == '/api/replay-archive-run' and write:
                        from .replay_archive_control import run_selected

                        if set(payload) != {'name','manifest_digest','confirmed'}:
                            raise ValueError('归档回放只接受名称、独立校验摘要和明确确认')
                        try:
                            result = run_selected(config.data_dir/'replay-archives',
                                name=payload['name'],manifest_digest=payload['manifest_digest'],confirmed=payload['confirmed'])
                        except (ValueError, OSError, TimeoutError, RuntimeError):
                            raise ValueError('归档回放未完成；请核对归档、独立摘要及运行状态，不要自动重试') from None
                    elif self.path == '/api/replay-archives' and write:
                        from .replay_archive import catalog

                        if set(payload) - {'limit'}:
                            raise ValueError('归档目录由服务端管理，不接受路径参数')
                        try:
                            result = catalog(config.data_dir/'replay-archives', limit=payload.get('limit', 50))
                        except FileNotFoundError:
                            result = {'items': [], 'truncated': False, 'read_only': True,
                                      'content_read': False, 'artifact_verification_performed': False}
                    elif self.path == "/api/backup-retention-history" and write:
                        from .backup_prune_audit import history

                        result = history(config, after_id=payload.get("after_id", ""), limit=20)
                    elif self.path == "/api/backup-retention-inspect" and write:
                        from .backup_prune_audit import inspect_receipt

                        result = inspect_receipt(config, receipt_id=payload.get("receipt_id"))
                    elif self.path == "/api/retention-inventory" and write:
                        from .retention_inventory import page

                        result = page(conn, state=payload.get("state", "all"), after_id=payload.get("after_id", ""))
                    elif self.path == '/api/body-retention-preview' and write:
                        from .body_retention import preview

                        result = preview(conn, days=payload.get('days'),
                                         after_id=payload.get('after_id', ''), limit=payload.get('limit', 30))
                    elif self.path == '/api/body-retention-clear' and write:
                        from .body_retention import clear_unreferenced_page

                        if payload.get('confirm_database_body_only') is not True:
                            raise ValueError('请明确确认仅清理数据库正文且不可撤销')
                        if payload.get('confirm_error_details') is not True:
                            raise ValueError('请刷新预览并确认清理范围包含错误详情')
                        result = clear_unreferenced_page(conn, days=payload.get('days'),
                            expected=payload.get('expected'), actor=config.control_operator_id,
                            after_id=payload.get('after_id', ''), limit=payload.get('limit', 30))
                    elif self.path == "/api/execution-recovery-preview" and write:
                        from .broker_recovery import preview

                        result = preview(conn, job_id=payload.get("job_id"))
                    elif self.path == "/api/execution-recovery" and write:
                        from .broker_recovery import apply

                        result = apply(conn, job_id=payload.get("job_id"), binding_digest=payload.get("binding_digest"),
                                       request_id=payload.get("request_id"), actor_id=config.control_operator_id)
                    elif self.path == "/api/remote-observe" and write:
                        from .broker_remote_observation import record

                        result = record(conn, config, request_id=payload.get("request_id"), observation_id=payload.get("observation_id"))
                    elif self.path == "/api/remote-cleanup-preview" and write:
                        from .broker_remote_cleanup import preview

                        result = preview(conn, config, request_id=payload.get("request_id"), observation_id=payload.get("observation_id"))
                    elif self.path == "/api/remote-cleanup-apply" and write:
                        from .broker_remote_cleanup import apply

                        result = apply(conn, config, request_id=payload.get("request_id"), observation_id=payload.get("observation_id"), preview_digest=payload.get("preview_digest"))
                    elif self.path == "/api/project-bugs/attachment-file" and write:
                        from .project_attachment_download import open_artifact

                        if set(payload) != {"activity_id"}:
                            raise ValueError("exact attachment identity required")
                        stream, receipt = open_artifact(conn, config, actor=config.control_operator_id, **payload)
                        with stream:
                            self.send_response(200)
                            self.send_header("Content-Type", "application/octet-stream")
                            self.send_header("Content-Disposition", 'attachment; filename="attachment.bin"')
                            self.send_header("Content-Length", str(receipt["size_bytes"]))
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("X-Content-Type-Options", "nosniff")
                            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
                            self.end_headers()
                            while chunk := stream.read(64 * 1024):
                                self.wfile.write(chunk)
                        return
                    elif self.path.startswith("/api/project-bugs/") and write:
                        from .project_bug_controls import execute

                        result = execute(conn, config, action=self.path.removeprefix("/api/project-bugs/"), payload=payload)
                    elif self.path == "/api/coding-task-options" and write:
                        from .coding_tasks import options
                        result = options(conn, config, payload)
                    elif self.path == "/api/coding-task" and write:
                        from .coding_tasks import submit
                        result = submit(conn, config, payload)
                    elif self.path == "/api/coding-executors" and write:
                        from .coding_catalog import choices
                        if payload:
                            raise ValueError("coding executor catalog takes no client settings")
                        result = choices(config)
                    elif self.path == "/api/executions" and write:
                        from .execution_inventory import page

                        result = page(conn, config, state=payload.get("state", "active"), after_id=payload.get("after_id", ""))
                    elif self.path == "/api/knowledge-lifecycle" and write:
                        from .knowledge_lifecycle import apply

                        result = apply(conn, knowledge_id=payload.get("knowledge_id"),
                                       content_digest=payload.get("content_digest"), decision=payload.get("decision"),
                                       actor_id=config.control_operator_id, request_id=payload.get("request_id"))
                    elif self.path == "/api/mail-digest-list" and write:
                        from .mail_digest_inventory import page

                        if set(payload) - {"after_id"}:
                            raise ValueError("unknown summary list argument")
                        result = page(conn, after_id=payload.get("after_id", ""))
                    elif self.path == "/api/mail-digest-detail" and write:
                        from .mail_digest_inventory import detail

                        if set(payload) - {"digest_id", "page", "expected_digest"}:
                            raise ValueError("unknown summary detail argument")
                        result = detail(conn, digest_id=payload.get("digest_id"),
                                        page=payload.get("page", 1), expected_digest=payload.get("expected_digest"))
                    elif self.path == "/api/release-impact-list" and write:
                        from .release_impact_inventory import page

                        if set(payload) - {"after_id"}:
                            raise ValueError("unknown impact list argument")
                        result = page(conn, after_id=payload.get("after_id", ""))
                    elif self.path == "/api/release-impact-detail" and write:
                        from .release_impact_inventory import detail

                        if set(payload) != {"impact_id"}:
                            raise ValueError("impact detail requires an ID")
                        result = detail(conn, impact_id=payload["impact_id"])
                    elif self.path == "/api/knowledge-list" and write:
                        from .knowledge_inventory import page

                        result = page(conn,query=payload.get("query",""),status=payload.get("status","all"),
                                      after_id=payload.get("after_id",""))
                    elif self.path == "/api/knowledge-detail" and write:
                        from .knowledge_preview import knowledge_preview

                        page = payload.get("page", 1)
                        if type(page) is not int or (page > 1 and not payload.get("content_digest")):
                            raise ValueError("knowledge pagination requires a content version")
                        shown = knowledge_preview(conn,knowledge_id=payload.get("knowledge_id"),page=page,
                                                  expected_digest=payload.get("content_digest"))
                        preview = shown["preview"]
                        result = {key:preview[key] for key in ("plain_text","page","page_count","content_digest")}
                        result["source_links"] = preview["source_links"]
                        result["read_only"] = True
                    elif self.path == "/api/budget-preview" and write:
                        from .budget_settings import preview

                        result = preview(conn,session_id=session["chat_id"],values=payload.get("values"),
                                         expected_revision=payload.get("expected_revision"))
                    elif self.path == "/api/budget-apply" and write:
                        from .budget_settings import apply

                        result = apply(conn,session_id=session["chat_id"],actor_id=config.control_operator_id,
                                       draft_id=payload.get("draft_id"))
                    elif self.path == "/api/model-budget" and not write:
                        from .model_budget import snapshot

                        result = snapshot(conn)
                    elif self.path == "/api/features" and not write:
                        from .feature_settings import view

                        result = view(conn, config)
                    elif self.path == "/api/mail-meeting-cancel" and write:
                        from .mail_meeting_prepare import cancel

                        result = cancel(conn, prepare_request_id=payload.get("prepare_request_id"),
                                        binding_digest=payload.get("binding_digest"),
                                        request_id=payload.get("request_id"),
                                        actor_id=config.control_operator_id)
                    elif self.path == "/api/mail-meeting-prepare" and write:
                        from .mail_meeting_prepare import enqueue

                        result = enqueue(conn, message_id=payload.get("message_id"),
                                         expected_revision=payload.get("expected_revision"),
                                         source_digest=payload.get("source_digest"),
                                         request_id=payload.get("request_id"),
                                         actor_id=config.control_operator_id)
                    elif self.path == "/api/mail-meeting-draft" and write:
                        from .mail_meeting_drafts import read

                        result = read(conn, payload.get("message_id"))
                    elif self.path == "/api/mail-meeting-draft-save" and write:
                        from .mail_meeting_drafts import save

                        result = save(conn, message_id=payload.get("message_id"),
                                      draft=payload.get("draft"),
                                      expected_revision=payload.get("expected_revision"),
                                      source_digest=payload.get("source_digest"),
                                      request_id=payload.get("request_id"),
                                      actor_id=config.control_operator_id)
                    elif self.path == "/api/attention-subscriptions" and write:
                        from .mail_catalog import CATEGORIES

                        rows = conn.execute("SELECT * FROM attention_subscriptions WHERE owner_id=? ORDER BY category", (config.control_operator_id,)).fetchall()
                        result = {"categories": sorted(CATEGORIES), "items": [dict(row) for row in rows], "read_only": True}
                    elif self.path == "/api/attention-subscription-save" and write:
                        from .attention_subscriptions import configure

                        result = configure(conn, owner_id=config.control_operator_id,
                                           category=payload.get("category"), enabled=payload.get("enabled"),
                                           expected_revision=payload.get("expected_revision"), request_id=payload.get("request_id"),
                                           snooze_minutes=payload.get("snooze_minutes"))
                    elif self.path == "/api/attention-collect" and write:
                        from .attention_subscriptions import collect

                        result = collect(conn, owner_id=config.control_operator_id)
                    elif self.path == "/api/attention-detail" and write:
                        from .attention_subscriptions import detail

                        result = detail(conn, owner_id=config.control_operator_id, action_id=payload.get("action_id"))
                    elif self.path == "/api/attention-list" and write:
                        from .attention_subscriptions import page

                        result = page(conn, owner_id=config.control_operator_id, after_id=payload.get("after_id", ""))
                    elif self.path == "/api/watch-settings" and write:
                        from .watch_subscriptions import settings

                        result = settings(conn, owner_id=config.control_operator_id,
                                          source_kind=payload.get("source_kind"), source_key=payload.get("source_key"))
                    elif self.path == "/api/watch-save" and write:
                        from .watch_subscriptions import configure

                        result = configure(conn, owner_id=config.control_operator_id,
                                           source_kind=payload.get("source_kind"), source_key=payload.get("source_key"),
                                           enabled=payload.get("enabled"), expected_revision=payload.get("expected_revision"),
                                           request_id=payload.get("request_id"), repositories=config.raw["repositories"])
                    elif self.path == "/api/watch-seen" and write:
                        from .watch_subscriptions import mark_seen

                        result = mark_seen(conn, owner_id=config.control_operator_id,
                                           action_id=payload.get("action_id"))
                    elif self.path == "/api/watch-list" and write:
                        from .watch_subscriptions import page

                        result = page(conn, owner_id=config.control_operator_id,
                                      after_id=payload.get("after_id", ""))
                    elif self.path == "/api/release-list" and write:
                        from .release_inventory import page

                        result = page(conn, repository=payload.get("repository", ""),
                                      after_id=payload.get("after_id", ""))
                    elif self.path == "/api/mail-list" and write:
                        from .mail_actions import page

                        result = page(conn, after_id=payload.get("after_id", ""),
                                      category=payload.get("category", "all"),
                                      state=payload.get("state", "all"))
                    elif self.path == "/api/mail-action" and write:
                        from .mail_actions import apply

                        result = apply(
                            conn, message_id=payload.get("message_id"),
                            action=payload.get("action"),
                            expected_revision=payload.get("expected_revision"),
                            content_digest=payload.get("content_digest"),
                            request_id=payload.get("request_id"),
                            actor_id=config.control_operator_id,
                            minutes=payload.get("minutes"), case_id=payload.get("case_id"),
                        )
                    elif self.path == "/api/notification-snooze" and write:
                        from .notification_snooze import set_snooze

                        result = set_snooze(
                            conn,
                            minutes=payload.get("minutes"),
                            expected_revision=payload.get("expected_revision"),
                            request_id=payload.get("request_id"),
                            actor_id=config.control_operator_id,
                            night_enabled=payload.get("night_enabled"),
                        )
                    elif self.path == "/api/features-preview" and write:
                        from .feature_settings import preview

                        result = preview(
                            conn,
                            config,
                            session_id=session["chat_id"],
                            values=payload.get("values"),
                            expected_revision=payload.get("expected_revision"),
                            rollback_revision=payload.get("rollback_revision"),
                            rebase=payload.get("rebase", False),
                        )
                    elif self.path == "/api/features-apply" and write:
                        from .feature_settings import apply

                        result = apply(
                            conn,
                            config,
                            session_id=session["chat_id"],
                            actor_id=config.control_operator_id,
                            draft_id=payload.get("draft_id"),
                        )
                    elif self.path == "/api/case-detail" and write:
                        result = app.detail(conn, payload, session)
                    elif self.path == "/api/case-content-inventory" and write:
                        from .case_content_inventory import bounded_preview

                        result = bounded_preview(conn, payload.get('case_id'))
                    elif self.path == "/api/sent-knowledge-preview" and write:
                        from .knowledge_use_preview import preview

                        result = preview(conn, case_id=payload.get("case_id"), use_id=payload.get("use_id"))
                    elif self.path == "/api/sent-knowledge-pending" and write:
                        from .knowledge_sent_feedback import pending

                        result = pending(conn, actor_id=config.control_operator_id,
                                         after_id=payload.get("after_id", ""), state=payload.get("state", "pending"))
                    elif self.path == "/api/sent-knowledge-review" and write:
                        from .knowledge_sent_feedback import review_feedback

                        result = review_feedback(conn, actor_id=config.control_operator_id,
                            feedback_id=payload.get("feedback_id"), decision=payload.get("decision"),
                            reason=payload.get("reason"), content_digest=payload.get("content_digest"),
                            request_id=payload.get("request_id"))
                    elif self.path == "/api/sent-knowledge-material" and write:
                        from .knowledge_sent_feedback import revision_material

                        result = revision_material(conn, actor_id=config.control_operator_id,
                                                   feedback_id=payload.get("feedback_id"))
                    elif self.path == "/api/sent-knowledge-feedback" and write:
                        from .knowledge_sent_feedback import apply

                        result = apply(conn, case_id=payload.get("case_id"), use_id=payload.get("use_id"),
                                       content_digest=payload.get("content_digest"), verdict=payload.get("verdict"),
                                       request_id=payload.get("request_id"), actor_id=config.control_operator_id)
                    elif self.path == "/api/workbench" and write:
                        result = workbench_page(
                            conn,
                            config,
                            view=payload.get("view", "all"),
                            cursor=payload.get("cursor"),
                            limit=30,
                            render_buttons=False,
                        )["snapshot"]
                    elif self.path == "/api/case-action" and write:
                        result = app.case_action(conn, session, payload)
                    elif self.path == "/api/approval-detail" and write:
                        result = app.approval_detail(conn, session, payload)
                    elif self.path == "/api/approval-action" and write:
                        result = app.approval_action(conn, session, payload)
                    elif self.path == "/api/panel" and write:
                        result = app.panel(conn, session)
                    elif self.path == "/api/action" and write:
                        result = app.action(
                            conn,
                            session,
                            payload.get("callback"),
                            payload.get("request_id"),
                        )
                    else:
                        return self.reply(404, {"error": "接口不存在"})
                    self.reply(200, result)
                finally:
                    conn.close()
            except PermissionError as exc:
                self.reply(403, {"error": str(exc)})
            except (
                ValueError,
                KeyError,
                TypeError,
                ApprovalError,
                ExecutorError,
                CalendarError,
                MeetingRecoveryError,
                ConflictError,
                NotFoundError,
            ) as exc:
                self.reply(409, {"error": str(exc)[:240]})
            except (sqlite3.Error, OSError):
                self.reply(
                    503, {"error": "控制服务暂不可用；未确认操作成功，请刷新核对"}
                )

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request(write=True)

    server = BoundedHTTPServer(("127.0.0.1", port), Handler, max_workers=max_workers)
    server.console = app
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--init-token", action="store_true")
    args = parser.parse_args()
    path = Path(args.token_file).expanduser()
    if not path.is_absolute():
        parser.error("token-file must be absolute")
    if args.init_token:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(secrets.token_hex(32))
        print(f"登录密钥已写入 {path}；请在本机读取后登录，不要转发。")
        return
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            parser.error("token file must be owner-only regular file")
        key = stream.read(128).strip()
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        parser.error("invalid login key")
    config = load_config(args.config)
    conn = connect(config.database_path)
    migrate(conn)
    conn.close()
    server = make_server(config, key, args.port)
    print(
        f"本地控制台：http://127.0.0.1:{server.server_port}（仅本机可访问）", flush=True
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
