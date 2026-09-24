from __future__ import annotations

from datetime import datetime

from k3_support.lark import CommandResult, LarkError
from k3_support.office_preflight import discover_group_candidates, office_doctor


def configured(config):
    config.raw["identity"]["feishu_owner_open_id"] = "ou_owner"
    config.raw["identity"]["feishu_p0_chat_id"] = "oc_owner_bot"
    config.raw["scope"]["technical_chat_ids"] = ["oc_support"]
    config.raw["base"] = {
        "app_token": "bas_token",
        "cases_table_id": "tbl_cases",
        "knowledge_table_id": "tbl_knowledge",
        "mail_table_id": "tbl_mail",
        "health_table_id": "tbl_health",
    }
    return config


def granted_p0_scopes():
    return {
        "scopes": [
            {"scope_name": "im:message.urgent", "grant_status": 1},
            {"scope_name": "im:message.urgent:sms", "grant_status": 1},
        ]
    }


def test_office_doctor_reports_independent_feature_readiness(config):
    cfg = configured(config)
    cfg.raw["notifications"]["feishu_app_urgent"] = True
    cfg.raw["notifications"]["feishu_sms_urgent"] = True

    def runner(argv, **_):
        if argv[:2] == ["im", "+messages-search"]:
            return CommandResult({"messages": []}, "user", [])
        if argv[:3] == ["im", "chats", "get"]:
            return CommandResult(
                {"chat_mode": "p2p", "owner_id": "ou_owner"}, "bot", []
            )
        if argv[:2] == ["base", "+table-list"]:
            return CommandResult(
                {
                    "tables": [
                        {"table_id": "tbl_cases"},
                        {"table_id": "tbl_knowledge"},
                        {"table_id": "tbl_mail"},
                        {"table_id": "tbl_health"},
                    ]
                },
                "user",
                [],
            )
        if argv[:2] == ["api", "GET"]:
            return CommandResult(granted_p0_scopes(), "bot", [])
        if argv[:2] == ["base", "+record-delete"]:
            assert "--dry-run" in argv
            return CommandResult({"api": []}, "user", [])
        raise AssertionError(argv)

    def mail_runner(argv, **_):
        assert argv[:2] == ["mail", "+triage"]
        return CommandResult({"messages": []}, "user", [])

    result = office_doctor(
        cfg,
        now=datetime.fromisoformat("2026-09-02T09:00:00+08:00"),
        runner=runner,
        mail_runner=mail_runner,
    )

    assert result["ready_for_shadow"] is True
    assert result["feature_readiness"] == {
        "mail": True,
        "p0": True,
        "base_sync": True,
        "requester_profile": True,
    }
    assert result["problems"] == []


def test_office_doctor_keeps_base_scope_failure_separate_from_shadow(config):
    cfg = configured(config)
    cfg.raw["scope"]["technical_chat_ids"] = []

    def runner(argv, **_):
        if argv[:2] == ["im", "+messages-search"]:
            return CommandResult({"messages": []}, "user", [])
        if argv[:3] == ["im", "chats", "get"]:
            return CommandResult(
                {"chat_mode": "p2p", "owner_id": "ou_owner"}, "bot", []
            )
        if argv[:2] == ["base", "+table-list"]:
            raise LarkError(
                "missing scope",
                error_type="authorization",
                subtype="missing_scope",
                missing_scopes=["base:app:read"],
            )
        if argv[:2] == ["api", "GET"]:
            return CommandResult(granted_p0_scopes(), "bot", [])
        if argv[:2] == ["base", "+record-delete"]:
            raise LarkError(
                "missing scope",
                error_type="authorization",
                subtype="missing_scope",
                missing_scopes=["base:record:delete"],
            )
        raise AssertionError(argv)

    result = office_doctor(
        cfg,
        now=datetime.fromisoformat("2026-09-02T09:00:00+08:00"),
        runner=runner,
        mail_runner=lambda *_args, **_kwargs: CommandResult(
            {"messages": []}, "user", []
        ),
    )

    assert result["ready_for_shadow"] is True
    assert result["feature_readiness"]["base_sync"] is False
    assert result["checks"]["base"]["missing_scopes"] == ["base:app:read"]
    assert result["warnings"]


def test_office_doctor_fails_p0_readiness_without_urgent_scopes(config):
    cfg = configured(config)
    cfg.raw["notifications"]["feishu_app_urgent"] = True
    cfg.raw["notifications"]["feishu_sms_urgent"] = True

    def runner(argv, **_):
        if argv[:2] == ["im", "+messages-search"]:
            return CommandResult({"messages": []}, "user", [])
        if argv[:3] == ["im", "chats", "get"]:
            return CommandResult(
                {"chat_mode": "p2p", "owner_id": "ou_owner"}, "bot", []
            )
        if argv[:2] == ["api", "GET"]:
            return CommandResult({"scopes": []}, "bot", [])
        if argv[:2] == ["base", "+table-list"]:
            return CommandResult(
                {
                    "tables": [
                        {"table_id": "tbl_cases"},
                        {"table_id": "tbl_knowledge"},
                        {"table_id": "tbl_mail"},
                        {"table_id": "tbl_health"},
                    ]
                },
                "user",
                [],
            )
        if argv[:2] == ["base", "+record-delete"]:
            raise LarkError(
                "missing scope",
                error_type="authorization",
                subtype="missing_scope",
                missing_scopes=["base:record:delete"],
            )
        raise AssertionError(argv)

    result = office_doctor(
        cfg,
        now=datetime.fromisoformat("2026-09-02T09:00:00+08:00"),
        runner=runner,
        mail_runner=lambda *_args, **_kwargs: CommandResult(
            {"messages": []}, "user", []
        ),
    )

    assert result["ready_for_shadow"] is True
    assert result["feature_readiness"]["p0"] is False
    assert result["checks"]["p0_route"]["ready"] is True
    assert result["checks"]["p0_urgent"]["ready"] is False
    assert result["checks"]["p0_urgent"]["channels"]["app"]["ready"] is False
    assert result["checks"]["p0_urgent"]["channels"]["sms"]["ready"] is False
    assert result["checks"]["base_cleanup"]["missing_scopes"] == ["base:record:delete"]


def test_office_doctor_skips_scope_probe_when_urgent_channels_are_disabled(config):
    cfg = configured(config)

    def runner(argv, **_):
        if argv[:2] == ["im", "+messages-search"]:
            return CommandResult({"messages": []}, "user", [])
        if argv[:3] == ["im", "chats", "get"]:
            return CommandResult(
                {"chat_mode": "p2p", "owner_id": "ou_owner"}, "bot", []
            )
        if argv[:2] == ["base", "+table-list"]:
            return CommandResult(
                {
                    "tables": [
                        {"table_id": "tbl_cases"},
                        {"table_id": "tbl_knowledge"},
                        {"table_id": "tbl_mail"},
                        {"table_id": "tbl_health"},
                    ]
                },
                "user",
                [],
            )
        if argv[:2] == ["base", "+record-delete"]:
            return CommandResult({}, "user", [])
        if argv[:2] == ["api", "GET"]:
            raise AssertionError("disabled urgent channels must not query app scopes")
        raise AssertionError(argv)

    result = office_doctor(
        cfg,
        now=datetime.fromisoformat("2026-09-02T09:00:00+08:00"),
        runner=runner,
        mail_runner=lambda *_args, **_kwargs: CommandResult(
            {"messages": []}, "user", []
        ),
    )

    assert result["feature_readiness"]["p0"] is True
    assert result["checks"]["p0_urgent"]["enabled"] is False
    assert result["checks"]["p0_urgent"]["channels"] == {
        "app": {"ready": True, "enabled": False},
        "sms": {"ready": True, "enabled": False},
    }


def test_office_doctor_requires_private_share_chat_for_mail_links(config):
    cfg = configured(config)
    cfg.raw["features"]["mail"] = True
    cfg.raw["mail"]["summary_share_chat_id"] = None
    cfg.raw["identity"]["feishu_p0_chat_id"] = None

    def runner(argv, **_):
        if argv[:2] == ["im", "+messages-search"]:
            return CommandResult({"messages": []}, "user", [])
        if argv[:3] == ["im", "chats", "get"]:
            return CommandResult(
                {"chat_mode": "p2p", "owner_id": "ou_owner"}, "bot", []
            )
        if argv[:2] == ["base", "+table-list"]:
            return CommandResult(
                {
                    "tables": [
                        {"table_id": "tbl_cases"},
                        {"table_id": "tbl_knowledge"},
                        {"table_id": "tbl_mail"},
                        {"table_id": "tbl_health"},
                    ]
                },
                "user",
                [],
            )
        if argv[:2] == ["base", "+record-delete"]:
            return CommandResult({}, "user", [])
        raise AssertionError(argv)

    result = office_doctor(
        cfg,
        now=datetime.fromisoformat("2026-09-02T09:00:00+08:00"),
        runner=runner,
        mail_runner=lambda *_args, **_kwargs: CommandResult(
            {"messages": []}, "user", []
        ),
    )

    assert result["feature_readiness"]["mail"] is False
    assert result["checks"]["mail_digest_links"] == {
        "ready": False,
        "enabled": True,
        "reason": "summary_share_chat_id is required for important-mail links",
    }


def test_office_doctor_reports_missing_optional_org_profile_scope(config):
    cfg = configured(config)
    cfg.raw["routing"]["org_profile_lookup"] = True

    def runner(argv, **_):
        if argv[:2] == ["im", "+messages-search"]:
            return CommandResult({"messages": []}, "user", [])
        if argv[:2] == ["api", "GET"]:
            raise LarkError(
                "missing scope",
                error_type="authorization",
                subtype="missing_scope",
                missing_scopes=["contact:contact.base:readonly"],
            )
        if argv[:3] == ["im", "chats", "get"]:
            return CommandResult(
                {"chat_mode": "p2p", "owner_id": "ou_owner"}, "bot", []
            )
        if argv[:2] == ["base", "+table-list"]:
            return CommandResult(
                {
                    "tables": [
                        {"table_id": "tbl_cases"},
                        {"table_id": "tbl_knowledge"},
                        {"table_id": "tbl_mail"},
                        {"table_id": "tbl_health"},
                    ]
                },
                "user",
                [],
            )
        if argv[:2] == ["base", "+record-delete"]:
            return CommandResult({}, "user", [])
        raise AssertionError(argv)

    result = office_doctor(
        cfg,
        now=datetime.fromisoformat("2026-09-02T09:00:00+08:00"),
        runner=runner,
        mail_runner=lambda *_args, **_kwargs: CommandResult(
            {"messages": []}, "user", []
        ),
    )

    assert result["ready_for_shadow"] is True
    assert result["feature_readiness"]["requester_profile"] is False
    assert result["checks"]["requester_profile"]["missing_scopes"] == [
        "contact:contact.base:readonly"
    ]


def test_scope_candidates_returns_metadata_only_and_marks_allowlist(config):
    cfg = configured(config)
    messages = [
        {
            "message_id": "om_1",
            "chat_id": "oc_support",
            "chat_name": "K3 Support",
            "create_time": "2026-09-01 12:00",
            "content": "must not be returned",
        },
        {
            "message_id": "om_2",
            "chat_id": "oc_support",
            "chat_name": "K3 Support",
            "create_time": "2026-09-02 08:00",
            "content": "also private",
        },
        {
            "message_id": "om_3",
            "chat_id": "oc_other",
            "chat_name": "Other",
            "create_time": "2026-08-30 08:00",
        },
    ]

    result = discover_group_candidates(
        cfg,
        days=30,
        now=datetime.fromisoformat("2026-09-02T09:00:00+08:00"),
        runner=lambda *_args, **_kwargs: CommandResult(
            {"messages": messages}, "user", []
        ),
    )

    assert result["message_count"] == 3
    assert result["candidate_count"] == 2
    assert result["candidates"][0] == {
        "chat_id": "oc_support",
        "chat_name": "K3 Support",
        "mention_count": 2,
        "latest": "2026-09-02 08:00",
        "configured": True,
    }
    assert "content" not in result["candidates"][0]
