import asyncio
import uuid
from types import SimpleNamespace

import pytest
import yaml
from test_coordination import make_turn
from test_hermes_control_plugin import FakeAdapter, wait_for_receipts, write_runtime

from k3_support.approvals import (
    ApprovalError,
    expiry_after,
    normalized_board_action,
    request_approval,
)
from k3_support.config import ConfigError, validate_config
from k3_support.control import ControlError, ControlMessage, execute_control
from k3_support.hermes_plugin import pre_gateway_dispatch


def setup_identity(config):
    config.raw["identity"].update(
        control_operator_id="shared-owner",
        feishu_control_user_id="tenant-user",
        feishu_control_chat_id="oc_control",
    )


def test_feishu_gateway_executes_real_cli_for_claim_and_exact_approval(
    conn, config, tmp_path, monkeypatch
):
    setup_identity(config)
    config.path.write_text(yaml.safe_dump(config.raw))
    runtime = write_runtime(tmp_path, config.path)
    monkeypatch.setenv("K3_SUPPORT_CONTROL_PLUGIN_CONFIG", str(runtime))
    case_id, _, _ = make_turn(conn)
    approval_id, _, _ = request_approval(
        conn,
        approval_type="board1_lease",
        case_id=case_id,
        session_id="feishu-test",
        action=normalized_board_action(case_id, "feishu-test", 15),
        expires_at=expiry_after(15),
    )

    async def exercise():
        adapter = FakeAdapter()
        gateway = SimpleNamespace(adapters={"feishu": adapter})

        async def command(text, *, user="tenant-user", chat="oc_control"):
            event = SimpleNamespace(
                text=text,
                message_id="om_" + uuid.uuid4().hex,
                source=SimpleNamespace(platform="feishu", user_id=user, chat_id=chat),
            )
            count = len(adapter.sent) + 1
            assert pre_gateway_dispatch(event, gateway)["action"] == "skip"
            await wait_for_receipts(adapter, count)
            return adapter.sent[-1][1]

        assert "身份不匹配" in await command(f"claim {case_id}", user="shared-owner")
        assert (
            conn.execute("SELECT count(*) FROM operator_activities").fetchone()[0] == 0
        )
        mode_receipt = await command("/feishu")
        pause = next(
            line
            for line in mode_receipt.splitlines()
            if line.startswith("mode-action ") and line.endswith(" p")
        )
        assert "立即暂停" in await command(pause)
        assert case_id in await command("workbench")
        await command(f"claim {case_id}")
        row = conn.execute(
            "SELECT actor_id,external_id FROM operator_activities"
        ).fetchone()
        assert row["actor_id"] == "shared-owner" and row["external_id"].startswith(
            "feishu:om_"
        )
        detail = await command(f"approval {approval_id}")
        approve = next(
            line for line in detail.splitlines() if line.startswith("approve ")
        )
        assert "未执行" in await command(approve, chat="oc_other")
        assert (
            conn.execute(
                "SELECT status FROM approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()[0]
            == "requested"
        )
        tampered = approve[:-1] + ("0" if approve[-1] != "0" else "1")
        assert "未执行" in await command(tampered)
        assert (
            conn.execute(
                "SELECT status FROM approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()[0]
            == "requested"
        )
        assert "已批准 board" in await command(approve)
        row = conn.execute(
            "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        assert (
            row["status"] == "approved"
            and row["approver_channel"] == "feishu"
            and row["approver_identity"] == "shared-owner"
        )
        # Feishu never enters the Telegram Bot API card sender.
        assert all(
            item[0] == "oc_control" or item[0] == "oc_other" for item in adapter.sent
        )

    asyncio.run(exercise())


def test_feishu_requires_explicit_channel_identity_and_fresh_approval(conn, config):
    setup_identity(config)
    message = ControlMessage(
        "tenant-user", "oc_control", "om_1", "approve apr_missing digest"
    )
    with pytest.raises(ControlError, match="fresh details"):
        execute_control(conn, config, message, control_channel="feishu")
    with pytest.raises(ApprovalError):
        execute_control(conn, config, message, control_channel="telegram")
    with pytest.raises(ControlError, match="unsupported Feishu"):
        execute_control(
            conn,
            config,
            ControlMessage(
                "tenant-user", "oc_control", "om_2", "mail-view anything anything"
            ),
            control_channel="feishu",
        )


def test_feishu_identity_configuration_must_be_complete(config):
    setup_identity(config)
    validate_config(config.raw)
    del config.raw["identity"]["feishu_control_chat_id"]
    with pytest.raises(ConfigError, match="Feishu control requires"):
        validate_config(config.raw)


def test_feishu_mode_commands_share_state_and_reject_old_panels(conn, config):
    from k3_support.runtime_control import (
        RuntimeControlError,
        bind_global_panel,
        execute_global_callback,
        issue_global_panel,
    )

    setup_identity(config)

    def command(text):
        return execute_control(
            conn,
            config,
            ControlMessage("tenant-user", "oc_control", uuid.uuid4().hex, text),
            control_channel="feishu",
        )

    def action(panel, code):
        return f"mode-action {panel['panel_id']} {panel['revision']} {code}"

    panel = command("/feishu")
    # A forged confirmation cannot enter automatic mode without a prior request.
    with pytest.raises(RuntimeControlError, match="二次确认"):
        command(action(panel, "A"))
    request = command(action(panel, "a"))
    assert request["mode"] != "auto"
    conn.execute(
        "UPDATE global_control_panels SET confirmation_expires_at='2000-01-01T00:00:00+00:00' WHERE panel_id=?",
        (panel["panel_id"],),
    )
    with pytest.raises(RuntimeControlError, match="二次确认"):
        command(action(request, "A"))
    request = command(action(panel, "a"))
    automatic = command(action(request, "A"))
    assert automatic["mode"] == "auto"
    with pytest.raises(RuntimeControlError, match="stale"):
        command(action(request, "A"))
    paused = command(action(automatic, "p"))
    assert paused["mode"] == "paused"
    # The web entry uses the same revision and retires the earlier Feishu panel.
    web = issue_global_panel(
        conn,
        config,
        operator_user_id="shared-owner",
        chat_id="web",
        command_message_id="web-mode-test",
        control_channel="gui",
    )
    bind_global_panel(
        conn,
        panel_id=web["panel_id"],
        operator_user_id="shared-owner",
        chat_id="web",
        command_message_id="web-mode-test",
        prompt_message_id="web-prompt",
    )
    with pytest.raises(RuntimeControlError, match="stale"):
        command(action(web, "o"))
    observed = execute_global_callback(
        conn,
        config,
        action="global_observe",
        panel_id=web["panel_id"],
        operator_user_id="shared-owner",
        chat_id="web",
        callback_query_id="web-click",
        prompt_message_id="web-prompt",
    )
    assert observed["mode"] == "observe"
    with pytest.raises(RuntimeControlError, match="stale"):
        command(action(paused, "c"))


def test_feishu_panel_migration_preserves_existing_rows(tmp_path, monkeypatch):
    from k3_support import db

    migrations = db.migration_files()
    connection = db.connect(tmp_path / "migration.db")
    try:
        monkeypatch.setattr(
            db,
            "migration_files",
            lambda: [item for item in migrations if item[0] < 100],
        )
        db.migrate(connection)
        for channel in ("telegram", "gui"):
            connection.execute(
                """INSERT INTO global_control_panels(
                panel_id,scope,operator_user_id,chat_id,command_message_id,prompt_message_id,
                expected_global_revision,pending_confirmation,confirmation_expires_at,state,
                created_at,updated_at,control_channel)
                VALUES(?,'feishu_support','owner','chat',?,'prompt',3,'auto','2099-01-01',
                'active','2026-01-01','2026-01-01',?)""",
                (channel, channel, channel),
            )
        before = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM global_control_panels ORDER BY panel_id"
            )
        ]
        monkeypatch.setattr(db, "migration_files", lambda: migrations)
        assert db.migrate(connection) == [item[0] for item in migrations if item[0] >= 100]
        assert before == [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM global_control_panels ORDER BY panel_id"
            )
        ]
        assert db.integrity(connection)["ok"]
    finally:
        connection.close()
