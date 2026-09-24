"""Exercise production panel transitions, including confirmation, in replay."""
from .runtime_control import (
    MODES,
    bind_global_panel,
    current_global_state,
    execute_global_callback,
    issue_global_panel,
)
from .workflow_replay import require_memory


def observe_mode_elapsed(conn, config, minutes):
    from datetime import timedelta

    from .timeutil import observed_clock, parse_iso

    require_memory(conn)
    if type(minutes) is not int or not 0 <= minutes <= 1440:
        raise ValueError('mode elapsed minutes must be an integer in 0..1440')
    state = conn.execute("SELECT mode,changed_at FROM global_control_state WHERE scope='feishu_support'").fetchone()
    if state is None or state['mode'] != 'auto_60':
        raise ValueError('elapsed mode observation requires an active auto_60 fixture')
    at = parse_iso(state['changed_at']) + timedelta(minutes=minutes)
    with observed_clock(at):
        result = current_global_state(conn, config)
    return {'mode': result['mode'], 'observed_at': at.isoformat(),
            'clock_scope': 'global_mode_only_not_worker_leases'}


def switch_mode(conn, config, *, mode, step_id):
    require_memory(conn)
    if mode not in MODES:
        raise ValueError('invalid replay mode')
    owner = config.telegram_control_user_id
    chat = config.telegram_control_chat_id
    if not owner or not chat:
        raise ValueError('mode scenarios require simulated owner and control chat')
    panel = issue_global_panel(conn, config, operator_user_id=owner, chat_id=chat,
                               command_message_id=f'replay-panel-{step_id}')
    prompt = f'replay-prompt-{step_id}'
    bind_global_panel(conn, panel_id=panel['panel_id'], operator_user_id=owner,
                      chat_id=chat, command_message_id=f'replay-panel-{step_id}', prompt_message_id=prompt)
    actions = {'observe': ['global_observe'], 'collaborate': ['global_collaborate'],
               'auto_60': ['global_auto_60'], 'paused': ['global_pause'],
               'auto': ['global_auto_request', 'global_auto_confirm'],
               'stopped': ['global_stop_request', 'global_stop_confirm']}[mode]
    for index, action in enumerate(actions):
        execute_global_callback(conn, config, action=action, panel_id=panel['panel_id'],
            operator_user_id=owner, chat_id=chat, callback_query_id=f'replay-{step_id}-{index}',
            prompt_message_id=prompt)
    return {'mode': current_global_state(conn, config)['mode'],
            'confirmation_scope': 'simulated_callbacks_not_live_approval'}
