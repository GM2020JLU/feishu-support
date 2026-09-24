from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import Config
from .lark import CommandResult, LarkError, run_json, run_mail_json

Runner = Callable[..., CommandResult]

P0_URGENT_SCOPE_ALTERNATIVES = {
    "app": {"im:message.urgent", "im:message.urgent:app_send"},
    "sms": {"im:message.urgent:sms", "im:message.urgent:sms_send"},
}


def _failure(exc: LarkError) -> dict[str, Any]:
    return {
        "ready": False,
        "error_type": exc.error_type,
        "subtype": exc.subtype,
        "missing_scopes": sorted(set(exc.missing_scopes)),
        "message": str(exc),
    }


def _messages(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        raise LarkError(
            "message search returned an invalid result", error_type="protocol"
        )
    values = data.get("messages")
    if not isinstance(values, list):
        raise LarkError(
            "message search returned no messages list", error_type="protocol"
        )
    return [value for value in values if isinstance(value, dict)]


def _granted_application_scopes(data: Any) -> set[str]:
    if not isinstance(data, dict) or not isinstance(data.get("scopes"), list):
        raise LarkError(
            "application scope query returned no scopes list", error_type="protocol"
        )
    return {
        str(item["scope_name"])
        for item in data["scopes"]
        if isinstance(item, dict)
        and isinstance(item.get("scope_name"), str)
        and item.get("grant_status") == 1
    }


def office_doctor(
    config: Config,
    *,
    now: datetime | None = None,
    runner: Runner = run_json,
    mail_runner: Runner = run_mail_json,
) -> dict[str, Any]:
    """Probe office integrations without writing or returning message content."""

    zone = ZoneInfo(config.raw["timezone"])
    end = now.astimezone(zone) if now is not None else datetime.now(zone)
    start = end - timedelta(minutes=2)
    checks: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    warnings: list[str] = []

    try:
        p2p = runner(
            [
                "im",
                "+messages-search",
                "--query",
                "",
                "--chat-type",
                "p2p",
                "--start",
                start.isoformat(timespec="seconds"),
                "--end",
                end.isoformat(timespec="seconds"),
                "--page-size",
                "1",
                "--format",
                "json",
                "--as",
                "user",
            ]
        )
        _messages(p2p.data)
        checks["user_message_poll"] = {
            "ready": True,
            "identity": p2p.identity,
            "coordination_reactions": True,
        }
    except LarkError as exc:
        checks["user_message_poll"] = _failure(exc)
        problems.append("user message polling is not ready")

    try:
        mail = mail_runner(
            [
                "mail",
                "+triage",
                "--folder",
                "INBOX",
                "--max",
                "1",
                "--format",
                "json",
                "--as",
                "user",
            ]
        )
        data = mail.data
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            raise LarkError(
                "mail triage returned no messages list", error_type="protocol"
            )
        checks["mail"] = {"ready": True, "identity": mail.identity}
    except LarkError as exc:
        checks["mail"] = _failure(exc)
        problems.append("Feishu Mail is not ready")

    mail_enabled = config.feature("mail")
    mail_share_chat = config.mail("summary_share_chat_id")
    if not mail_enabled:
        checks["mail_digest_links"] = {
            "ready": True,
            "enabled": False,
            "reason": "mail summaries are disabled",
        }
    elif not mail_share_chat:
        checks["mail_digest_links"] = {
            "ready": False,
            "enabled": True,
            "reason": "summary_share_chat_id is required for important-mail links",
        }
        problems.append("important-mail link destination is not configured")
    else:
        try:
            target = runner(
                [
                    "im", "chats", "get", "--chat-id", str(mail_share_chat),
                    "--as", "user", "--format", "json",
                ]
            )
            checks["mail_digest_links"] = {
                "ready": target.identity == "user",
                "enabled": True,
                "identity": target.identity,
            }
            if target.identity != "user":
                problems.append("important-mail link destination is not user-accessible")
        except LarkError as exc:
            checks["mail_digest_links"] = _failure(exc)
            checks["mail_digest_links"]["enabled"] = True
            problems.append("important-mail link destination is not ready")

    owner_id = config.raw["identity"].get("feishu_owner_open_id")
    organization_enabled = config.raw["routing"]["org_profile_lookup"]
    if not organization_enabled:
        checks["requester_profile"] = {
            "ready": True,
            "enabled": False,
            "reason": "organization profile lookup is disabled",
        }
    elif not owner_id:
        checks["requester_profile"] = {
            "ready": False,
            "enabled": True,
            "reason": "feishu_owner_open_id is required",
        }
        problems.append("requester organization profile lookup is not configured")
    else:
        try:
            result = runner(
                [
                    "api",
                    "GET",
                    f"/open-apis/contact/v3/users/{owner_id}",
                    "--params",
                    '{"department_id_type":"open_department_id","user_id_type":"open_id"}',
                    "--as",
                    "user",
                    "--format",
                    "json",
                ]
            )
            data = result.data if isinstance(result.data, dict) else {}
            if isinstance(data.get("data"), dict):
                data = data["data"]
            user = data.get("user") if isinstance(data, dict) else None
            required_fields = {"department_ids", "job_title", "leader_user_id"}
            ready = (
                result.identity == "user"
                and isinstance(user, dict)
                and user.get("open_id") == owner_id
                and required_fields <= set(user)
            )
            checks["requester_profile"] = {
                "ready": ready,
                "enabled": True,
                "identity": result.identity,
                "owner_matches": isinstance(user, dict)
                and user.get("open_id") == owner_id,
                "required_fields_present": isinstance(user, dict)
                and required_fields <= set(user),
            }
            if not ready:
                problems.append("requester organization fields are incomplete")
        except LarkError as exc:
            checks["requester_profile"] = _failure(exc)
            checks["requester_profile"]["enabled"] = True
            problems.append("requester organization profile lookup is not ready")

    p0_chat_id = config.raw["identity"].get("feishu_p0_chat_id")
    feishu_p0_enabled = config.notification("feishu_p0_message")
    if not feishu_p0_enabled:
        checks["p0_route"] = {
            "ready": True,
            "enabled": False,
            "reason": "ordinary Feishu P0 messages are disabled",
        }
    elif not owner_id or not p0_chat_id:
        checks["p0_route"] = {
            "ready": False,
            "enabled": True,
            "reason": "feishu_owner_open_id and feishu_p0_chat_id are required",
        }
        problems.append("Feishu P0 route is not configured")
    else:
        try:
            result = runner(
                [
                    "im",
                    "chats",
                    "get",
                    "--chat-id",
                    str(p0_chat_id),
                    "--as",
                    "bot",
                    "--format",
                    "json",
                ]
            )
            data = result.data if isinstance(result.data, dict) else {}
            ready = (
                result.identity == "bot"
                and data.get("chat_mode") == "p2p"
                and data.get("owner_id") == owner_id
            )
            checks["p0_route"] = {
                "ready": ready,
                "enabled": True,
                "identity": result.identity,
                "chat_mode": data.get("chat_mode"),
                "owner_matches": data.get("owner_id") == owner_id,
            }
            if not ready:
                problems.append(
                    "Feishu P0 route does not resolve to the configured owner"
                )
        except LarkError as exc:
            checks["p0_route"] = _failure(exc)
            problems.append("Feishu P0 route is not ready")

    enabled_urgent = {
        "app": config.notification("feishu_app_urgent"),
        "sms": config.notification("feishu_sms_urgent"),
    }
    if not any(enabled_urgent.values()):
        checks["p0_urgent"] = {
            "ready": True,
            "enabled": False,
            "channels": {
                name: {"ready": True, "enabled": False}
                for name in P0_URGENT_SCOPE_ALTERNATIVES
            },
        }
    else:
        try:
            result = runner(
                [
                    "api",
                    "GET",
                    "/open-apis/application/v6/scopes",
                    "--as",
                    "bot",
                    "--format",
                    "json",
                ]
            )
            granted = _granted_application_scopes(result.data)
            channels = {
                name: {
                    "ready": not enabled_urgent[name]
                    or bool(alternatives & granted),
                    "enabled": enabled_urgent[name],
                    "required_any_of": sorted(alternatives),
                    "granted": sorted(alternatives & granted),
                }
                for name, alternatives in P0_URGENT_SCOPE_ALTERNATIVES.items()
            }
            checks["p0_urgent"] = {
                "ready": all(channel["ready"] for channel in channels.values()),
                "enabled": True,
                "identity": result.identity,
                "channels": channels,
            }
            if not checks["p0_urgent"]["ready"]:
                problems.append("Feishu P0 urgent permissions are not ready")
        except LarkError as exc:
            checks["p0_urgent"] = _failure(exc)
            checks["p0_urgent"]["enabled"] = True
            problems.append("Feishu P0 urgent permissions cannot be verified")

    base = config.raw["base"]
    expected_tables = {
        value
        for key in (
            "cases_table_id",
            "knowledge_table_id",
            "mail_table_id",
            "health_table_id",
        )
        if (value := base.get(key))
    }
    if not base.get("app_token") or len(expected_tables) != 4:
        checks["base"] = {
            "ready": False,
            "reason": "Base and four table IDs are required",
        }
        problems.append("Feishu Base mirror is not configured")
    else:
        try:
            result = runner(
                [
                    "base",
                    "+table-list",
                    "--base-token",
                    str(base["app_token"]),
                    "--limit",
                    "100",
                    "--format",
                    "json",
                    "--as",
                    "user",
                ]
            )
            data = result.data if isinstance(result.data, dict) else {}
            tables = data.get("tables")
            if not isinstance(tables, list):
                raise LarkError(
                    "Base table list returned no tables list", error_type="protocol"
                )
            actual = {
                str(item.get("table_id") or item.get("id"))
                for item in tables
                if isinstance(item, dict) and (item.get("table_id") or item.get("id"))
            }
            missing = sorted(expected_tables - actual)
            checks["base"] = {
                "ready": not missing,
                "identity": result.identity,
                "configured_table_count": 4,
                "matched_table_count": 4 - len(missing),
                "missing_table_ids": missing,
            }
            if missing:
                problems.append("Feishu Base is missing configured tables")
        except LarkError as exc:
            checks["base"] = _failure(exc)
            problems.append("Feishu Base mirror is not ready")

        try:
            cleanup = runner(
                [
                    "base",
                    "+record-delete",
                    "--base-token",
                    str(base["app_token"]),
                    "--table-id",
                    str(base["cases_table_id"]),
                    "--record-id",
                    "rec_office_doctor_scope_probe",
                    "--as",
                    "user",
                    "--dry-run",
                    "--format",
                    "json",
                ]
            )
            checks["base_cleanup"] = {
                "ready": cleanup.identity == "user",
                "identity": cleanup.identity,
            }
        except LarkError as exc:
            checks["base_cleanup"] = _failure(exc)
            warnings.append(
                "Base record cleanup is not ready; canary records cannot be removed"
            )

    technical_count = len(config.raw["scope"]["technical_chat_ids"])
    auto_reply_count = len(config.raw["scope"]["auto_reply_chat_ids"])
    checks["group_scope"] = {
        "ready": technical_count > 0,
        "technical_chat_count": technical_count,
        "auto_reply_chat_count": auto_reply_count,
    }
    if technical_count == 0:
        warnings.append(
            "no technical group is allowlisted; P2P polling still works but group @mentions are ignored"
        )

    return {
        "ready_for_shadow": checks["user_message_poll"]["ready"]
        and checks["mail"]["ready"],
        "feature_readiness": {
            "mail": checks["mail"]["ready"] and checks["mail_digest_links"]["ready"],
            "p0": checks["p0_route"]["ready"] and checks["p0_urgent"]["ready"],
            "base_sync": checks["base"]["ready"],
            "requester_profile": checks["requester_profile"]["ready"],
        },
        "checks": checks,
        "problems": problems,
        "warnings": warnings,
    }


def discover_group_candidates(
    config: Config,
    *,
    days: int = 30,
    now: datetime | None = None,
    runner: Runner = run_json,
) -> dict[str, Any]:
    """Return group IDs that actually mentioned the operator, without message text."""

    if not 1 <= days <= 90:
        raise ValueError("days must be between 1 and 90")
    zone = ZoneInfo(config.raw["timezone"])
    end = now.astimezone(zone) if now is not None else datetime.now(zone)
    start = end - timedelta(days=days)
    result = runner(
        [
            "im",
            "+messages-search",
            "--query",
            "",
            "--chat-type",
            "group",
            "--is-at-me",
            "--start",
            start.isoformat(timespec="seconds"),
            "--end",
            end.isoformat(timespec="seconds"),
            "--page-size",
            "50",
            "--page-all",
            "--no-reactions",
            "--format",
            "json",
            "--as",
            "user",
        ]
    )
    configured = set(config.raw["scope"]["technical_chat_ids"])
    grouped: dict[str, dict[str, Any]] = {}
    for item in _messages(result.data):
        chat_id = item.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id:
            continue
        candidate = grouped.setdefault(
            chat_id,
            {
                "chat_id": chat_id,
                "chat_name": item.get("chat_name") or "",
                "mention_count": 0,
                "latest": "",
                "configured": chat_id in configured,
            },
        )
        candidate["mention_count"] += 1
        created = str(item.get("create_time") or "")
        candidate["latest"] = max(candidate["latest"], created)
    candidates = sorted(
        grouped.values(),
        key=lambda item: (item["latest"], item["mention_count"]),
        reverse=True,
    )
    return {
        "identity": result.identity,
        "window_start": start.isoformat(timespec="seconds"),
        "window_end": end.isoformat(timespec="seconds"),
        "message_count": sum(item["mention_count"] for item in candidates),
        "candidate_count": len(candidates),
        "configured_candidate_count": sum(
            bool(item["configured"]) for item in candidates
        ),
        "candidates": candidates,
    }
