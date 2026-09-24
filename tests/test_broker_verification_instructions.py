from k3_support.broker_verification_instructions import task_guidance


def test_ordinary_coding_can_test_without_prepared_verification_request():
    text = task_guidance({"repos": ["calculator"]})
    assert "Use scoped k3_remote work requests" in text
    assert "empty verification_list" in text
    assert "Only submit dispatchable items" not in text


def test_planned_verification_keeps_exact_prepared_command_gate():
    text = task_guidance({"verification": {"run_id": "run-1"}})
    assert "Only submit dispatchable items" in text
    assert "remote_request_id unchanged" in text
