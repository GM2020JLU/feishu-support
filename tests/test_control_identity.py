import pytest
from test_coordination import make_turn

from k3_support.approvals import ApprovalError
from k3_support.control import ControlMessage, execute_control


def test_telegram_claim_records_shared_operator_after_authentication(conn, config):
    config.raw['identity']['control_operator_id'] = 'shared-owner'
    case_id, _, _ = make_turn(conn)
    command = f'claim {case_id}'
    with pytest.raises(ApprovalError):
        execute_control(conn, config, ControlMessage('shared-owner', 'web', 'forged', command))
    assert conn.execute('SELECT count(*) FROM operator_activities').fetchone()[0] == 0
    message = ControlMessage(config.telegram_control_user_id, config.telegram_control_chat_id,
                             'original-message', command)
    execute_control(conn, config, message)
    row = dict(conn.execute('SELECT * FROM operator_activities').fetchone())
    assert row['actor_id'] == 'shared-owner'
    assert row['external_id'] == 'telegram:original-message'
    execute_control(conn, config, message)
    assert conn.execute('SELECT count(*) FROM operator_activities').fetchone()[0] == 1
