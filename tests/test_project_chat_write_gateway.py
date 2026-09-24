"""Real gateway -> control CLI -> queue; channel delivery and provider stay offline."""

# ruff: noqa: F811
import asyncio
from types import SimpleNamespace

import pytest
import yaml
from test_hermes_control_plugin import FakeAdapter, wait_for_receipts, write_runtime
from test_project_bug_operations import setup  # noqa: F401
from test_project_chat_control import message
from test_project_comment_dispatch import ready as comment_ready
from test_project_write_dispatch import ready as field_ready

from k3_support.hermes_plugin import pre_gateway_dispatch


@pytest.mark.parametrize("channel", ["feishu", "telegram"])
@pytest.mark.parametrize("kind", ["comment", "field"])
def test_gateway_previews_and_queues_exact_write_once(
    conn, setup, tmp_path, monkeypatch, channel, kind
):
    cfg, op, _, native = (comment_ready if kind == "comment" else field_ready)(
        conn, setup, monkeypatch
    )
    preview = message(cfg, "bug write " + op["operation_id"], channel, "view-write")
    send = message(
        cfg,
        "bug send-write " + op["operation_id"] + " " + op["request_digest"],
        channel,
        "send-write",
    )
    cfg.path.write_text(yaml.safe_dump(cfg.raw))
    runtime = write_runtime(tmp_path, cfg.path)
    monkeypatch.setenv("K3_SUPPORT_CONTROL_PLUGIN_CONFIG", str(runtime))

    async def run():
        adapter = FakeAdapter()
        gateway = SimpleNamespace(adapters={channel: adapter})
        for count, msg in enumerate([preview, send, send], 1):
            event = SimpleNamespace(
                text=msg.text,
                message_id=msg.message_id,
                raw_message=None,
                source=SimpleNamespace(
                    platform=channel, user_id=msg.user_id, chat_id=msg.chat_id
                ),
            )
            assert pre_gateway_dispatch(event, gateway)["action"] == "skip"
            await wait_for_receipts(adapter, count)
        assert op["operation_id"] in adapter.sent[0][1]
        assert op["request_digest"] in adapter.sent[0][1]
        assert all("控制命令未执行" not in row[1] for row in adapter.sent)

    asyncio.run(run())
    table = (
        "project_comment_dispatch_requests"
        if kind == "comment"
        else "project_write_dispatch_requests"
    )
    assert conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 1
    assert (
        conn.execute(
            "SELECT state FROM project_bug_operations WHERE operation_id=?",
            (op["operation_id"],),
        ).fetchone()[0]
        == "prepared"
    )
    assert (
        conn.execute("SELECT count(*) FROM project_write_attempts").fetchone()[0] == 0
    )
    assert (
        conn.execute("SELECT count(*) FROM project_comment_attempts").fetchone()[0] == 0
    )
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert not (native.writes if kind == "comment" else native.updates)


@pytest.mark.parametrize("channel", ["feishu", "telegram"])
@pytest.mark.parametrize("first", ["round", "write"])
def test_edited_message_cannot_switch_between_task_and_write(
    conn, setup, monkeypatch, channel, first
):
    from k3_support.control import execute_control
    from k3_support.project_bugs import BugConflict, detail

    cfg, op, _, _ = field_ready(conn, setup, monkeypatch)
    if first == "round":
        # A new round correctly refuses unsettled writes. Settle the fixture
        # before opening the round; the edited message must still hit the
        # shared native-intent fence before it can attempt the other command.
        from k3_support import project_bug_operations

        project_bug_operations.cancel(
            conn, operation_id=op["operation_id"], actor="owner",
            expected_digest=op["request_digest"],
        )
    bug = detail(conn, op["bug_id"])
    commands = {
        "round": f'bug start-round {bug["bug_id"]} {bug["revision"]} "new investigation"',
        "write": f"bug send-write {op['operation_id']} {op['request_digest']}",
    }
    execute_control(
        conn,
        cfg,
        message(cfg, commands[first], channel, "one-native-message"),
        control_channel=channel,
    )
    counts = tuple(
        conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
        for table in ("project_bug_rounds", "project_write_dispatch_requests")
    )
    other = "write" if first == "round" else "round"
    with pytest.raises(BugConflict, match="native message ID.*different"):
        execute_control(
            conn,
            cfg,
            message(cfg, commands[other], channel, "one-native-message"),
            control_channel=channel,
        )
    assert (
        tuple(
            conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
            for table in ("project_bug_rounds", "project_write_dispatch_requests")
        )
        == counts
    )
