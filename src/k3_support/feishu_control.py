"""Authenticated Feishu text controls over the shared versioned mode panel."""

from .db import transaction
from .runtime_control import (
    RuntimeControlError,
    bind_global_panel,
    execute_global_callback,
    issue_global_panel,
)

_ACTIONS = {
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
}


def route_mode(conn, config, message, argv):
    # execute_control authenticates the native user/chat before reaching here.
    # Bind text commands to the opening message and panel revision. A receipt
    # need not be edited in place: old revision commands fail after a transition.
    with transaction(conn):
        if argv == ["mode"]:
            command_id = f"feishu:{message.chat_id}:{message.message_id}"
            result = issue_global_panel(
                conn,
                config,
                operator_user_id=message.user_id,
                chat_id=message.chat_id,
                command_message_id=command_id,
                control_channel="feishu",
            )
            bind_global_panel(
                conn,
                panel_id=result["panel_id"],
                operator_user_id=message.user_id,
                chat_id=message.chat_id,
                command_message_id=command_id,
                prompt_message_id=command_id,
            )
        elif len(argv) == 4 and argv[0] == "mode-action" and argv[3] in _ACTIONS:
            panel = conn.execute(
                "SELECT * FROM global_control_panels WHERE panel_id=?", (argv[1],)
            ).fetchone()
            if (
                panel is None
                or panel["control_channel"] != "feishu"
                or str(panel["expected_global_revision"]) != argv[2]
            ):
                raise RuntimeControlError("mode command is stale; open mode again")
            result = execute_global_callback(
                conn,
                config,
                action=_ACTIONS[argv[3]],
                panel_id=argv[1],
                operator_user_id=message.user_id,
                chat_id=message.chat_id,
                callback_query_id=f"{message.chat_id}:{message.message_id}",
                prompt_message_id=panel["prompt_message_id"],
            )
        else:
            raise RuntimeControlError(
                "usage: mode or mode-action <panel> <revision> <action>"
            )
        commands = []
        for button in result["buttons"]:
            _, action, panel_id = button["callback_data"].split(":", 2)
            if action in _ACTIONS:
                commands.append(
                    f"{button['text']}\nmode-action {panel_id} {result['revision']} {action}"
                )
        return {
            **result,
            "command": "feishu_mode",
            "commands": [*commands, "workbench"],
        }
