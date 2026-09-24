from uuid import uuid4
import json

import pytest
from test_broker_grant_schema import grant
from test_broker_task_binding import seed

from k3_support.board_test_evidence import items, lines


def setup(conn, state="succeeded", code=0):
    binding = seed(conn)
    grant(conn)
    request = str(uuid4())
    conn.execute("INSERT INTO broker_board_actions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (request, 1234, "grant-1", "fixture-session", "a"*64,
                  '{"type":"serial_exec","command":"PRIVATE COMMAND"}', "b"*64, "c"*64, state, "now", "now"))
    if code is not None:
        conn.execute("INSERT INTO broker_board_results VALUES(?,?,?,?,?)",
                     (request, code, "PRIVATE SERIAL BODY", "PRIVATE STDERR", "receipt-time"))
    return binding["case_id"], request


@pytest.mark.parametrize("state,code,label", [("succeeded", 0, "命令正常退出"), ("succeeded", None, "记录不一致"),
    ("unknown", 124, "结果未知"), ("queued", None, "尚未执行"), ("failed", 255, "记录不一致")])
def test_receipts_are_scoped_readonly_and_never_claim_environment_success(conn, state, code, label):
    case, _ = setup(conn, state, code)
    before = conn.serialize()
    records = items(conn, case_id=case, lifecycle_round=1)
    assert label in records[0]["label"]
    assert not records[0]["repair_verified"] and not records[0]["environment_verified"]
    rendered = "\n".join(lines(conn, case_id=case, lifecycle_round=1))
    assert "PRIVATE" not in rendered
    assert items(conn, case_id="other", lifecycle_round=1) == []
    assert items(conn, case_id=case, lifecycle_round=2) == []
    assert conn.serialize() == before


def test_receipt_changes_invalidate_snapshot_without_revealing_body(conn):
    case, request = setup(conn)
    before = items(conn, case_id=case, lifecycle_round=1)
    conn.execute("UPDATE broker_board_results SET stdout='changed private log' WHERE request_id=?", (request,))
    after = items(conn, case_id=case, lifecycle_round=1)
    assert before[0]["receipt_digest"] != after[0]["receipt_digest"]
    assert "changed private log" not in str(after)
    conn.execute("UPDATE cases SET lifecycle_round=2 WHERE case_id=?", (case,))
    assert items(conn, case_id=case, lifecycle_round=2) == []


def test_changed_receipt_invalidates_case_detail_preview(conn):
    from k3_support.case_detail import case_detail

    case, request = setup(conn)
    first = case_detail(conn, case_id=case)["preview"]
    conn.execute("UPDATE broker_board_results SET stdout='late changed output' WHERE request_id=?", (request,))
    with pytest.raises(ValueError, match="stale"):
        case_detail(conn, case_id=case, expected_digest=first["content_digest"])


def test_receipt_is_reachable_through_detail_pagination(conn):
    from k3_support.case_detail import case_detail

    case, request = setup(conn)
    before = conn.serialize()
    first = case_detail(conn, case_id=case)["preview"]
    pages = [case_detail(conn, case_id=case, page=page,
                        expected_digest=first["content_digest"])["preview"]
             for page in range(1, first["page_count"] + 1)]
    rendered = "\n".join(page["plain_text"] for page in pages)
    assert "本轮板卡操作回执" in rendered
    assert request in rendered
    assert "命令成功不等于启动正常" in rendered
    assert "PRIVATE" not in rendered
    assert all(page["content_digest"] == first["content_digest"] for page in pages)
    assert conn.serialize() == before


@pytest.mark.parametrize('fresh,action,state', [(True, 'serial_wait', 'succeeded'),
    (False, 'serial_wait', 'succeeded'), (True, 'serial_exec', 'succeeded'),
    (True, 'serial_wait', 'unknown')])
def test_boot_markers_require_successful_fresh_serial_receipt(conn, fresh, action, state):
    case, request = setup(conn, state=state)
    output = 'PRIVATE LOG\nU-Boot 2024.01 private-build-details\n'
    receipt = json.dumps({'ok': True, 'matched': True, 'fresh': fresh,
                          'rx_seq_start': 42, 'output': output})
    conn.execute('UPDATE broker_board_actions SET action_json=?', (json.dumps({'type': action}),))
    conn.execute('UPDATE broker_board_results SET stdout=? WHERE request_id=?', (receipt, request))
    before = conn.serialize()
    row = items(conn, case_id=case, lifecycle_round=1)[0]
    expected = fresh and action == 'serial_wait' and state == 'succeeded'
    assert bool(row['boot_markers']) == expected
    if expected:
        marker = row['boot_markers'][0]
        assert marker['marker'] == 'u-boot'
        assert output[marker['start']:marker['end']].startswith('U-Boot 2024.01')
    assert not row['environment_verified'] and not row['repair_verified']
    rendered = '\n'.join(lines(conn, case_id=case, lifecycle_round=1))
    assert 'private-build-details' not in rendered and 'PRIVATE LOG' not in rendered
    assert conn.serialize() == before


@pytest.mark.parametrize('text', ['echo U-Boot 2024.01', '"U-Boot 2024.01"',
    'Example: Linux version 6.6', 'U-Boot unknown-version'])
def test_unanchored_or_unknown_banner_is_not_a_marker(text):
    from k3_support.board_serial_evidence import boot_markers
    assert boot_markers(json.dumps({'ok': True, 'matched': True, 'fresh': True,
                                   'rx_seq_start': 0, 'output': text})) == []


def test_boot_markers_keep_multiple_stages_without_selecting_current_one():
    from k3_support.board_serial_evidence import boot_markers
    text = 'U-Boot SPL 2024.01\nOpenSBI v1.4\nU-Boot 2024.01\nLinux version 6.6\n'
    markers = boot_markers(json.dumps({'ok': True, 'matched': True, 'fresh': True,
                                      'rx_seq_start': 0, 'output': text}))
    assert {m['marker'] for m in markers} == {'spl', 'opensbi', 'u-boot', 'linux'}
    assert all(m['scope'] == 'historical_serial_marker_not_current_environment' for m in markers)
