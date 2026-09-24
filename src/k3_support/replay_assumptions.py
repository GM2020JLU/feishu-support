"""Explicit scenario assumptions applied only to memory replay databases."""

from contextlib import nullcontext

from .routing import FUNCTION_ROLES, RELATIONSHIPS, set_requester_profile
from .runtime_control import MODES
from .timeutil import parse_iso, observed_clock


def validate(value):
    if not isinstance(value, dict) or set(value) - {'relationship', 'function_role', 'mode', 'observed_at'}:
        raise ValueError('invalid replay assumptions')
    for key, allowed in [('relationship', RELATIONSHIPS), ('function_role', FUNCTION_ROLES), ('mode', MODES)]:
        if key in value and (not isinstance(value[key], str) or value[key] not in allowed):
            raise ValueError('invalid replay assumption: ' + key)
    if ('relationship' in value) != ('function_role' in value):
        raise ValueError('assumed relationship and function role must be supplied together')
    if 'observed_at' in value:
        at = value['observed_at']
        if not isinstance(at, str) or not 1 <= len(at) <= 64:
            raise ValueError('invalid replay observation time')
        parse_iso(at)
    return dict(value)


def clock(value):
    value = validate(value)
    return observed_clock(parse_iso(value['observed_at'])) if 'observed_at' in value else nullcontext()


def apply(conn, config, event, value):
    from .workflow_replay import require_memory
    from .replay_modes import switch_mode

    require_memory(conn)
    value = validate(value)
    if 'relationship' in value:
        sender = event.get('sender_id')
        if not isinstance(sender, str) or not 1 <= len(sender) <= 256:
            raise ValueError('assumed profile requires a sender ID')
        set_requester_profile(conn, requester_id=sender,
                              relationship=value['relationship'], function_role=value['function_role'],
                              source='operator', evidence={'scenario_assumption': True})
    if 'mode' in value:
        switch_mode(conn, config, mode=value['mode'], step_id='snapshot-assumption')
    return value
