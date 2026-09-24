import json

import pytest

from test_release_crossreview import _case_reply
from k3_support.board_test_evidence import reviewed_versions, lines


def test_new_round_board_evidence_visible_with_retired_history(conn, config, monkeypatch):
    import test_review
    from k3_support.case_detail import case_detail
    original = test_review.create_case
    def new_round(*args, **kwargs):
        result = original(*args, **kwargs)
        cid = result[0]
        conn.execute('UPDATE cases SET lifecycle_round=2 WHERE case_id=?', (cid,))
        conn.execute('''INSERT INTO case_rounds(case_id,round_number,started_at,reason,initial_case_version)
            VALUES(?,2,'now','synthetic new round',1)''', (cid,))
        conn.execute('INSERT INTO case_content_retirements VALUES(?,?,?,?,?,?,?)',
                     (cid,1,'old-board-round','a'*64,'b'*64,'now','fixture'))
        return result
    monkeypatch.setattr(test_review,'create_case',new_round)
    _case_reply(conn,config,'board',serial_receipt=json.dumps(dict(ok=True,fresh=True,matched=True,
        rx_seq_start=10,output='PRIVATE LOG\nU-Boot 2025.01-k3\nTrying to boot from MMC1\n')))
    cid = conn.execute('SELECT case_id FROM cases LIMIT 1').fetchone()[0]
    before = conn.serialize()
    shown = case_detail(conn,case_id=cid)['preview']
    combined = ''.join(case_detail(conn,case_id=cid,page=page,
        expected_digest=shown['content_digest'])['preview']['plain_text']
        for page in range(1,shown['page_count']+1))
    assert '2025.01-k3' in combined and '尝试 MMC1' in combined
    assert 'PRIVATE LOG' not in combined and 'old-board-round' in combined
    assert conn.serialize() == before
    conn.execute("UPDATE action_ledger SET result_json=json_set(result_json,'$.stdout','changed') WHERE action_type='board'")
    with pytest.raises(ValueError,match='stale'):
        case_detail(conn,case_id=cid,expected_digest=shown['content_digest'])
    changed = case_detail(conn,case_id=cid)['preview']
    assert '2025.01-k3' not in changed['plain_text']
    conn.execute('UPDATE cases SET lifecycle_round=3 WHERE case_id=?', (cid,))
    conn.execute('INSERT INTO case_content_retirements VALUES(?,?,?,?,?,?,?)',
                 (cid,2,'second-board-round','a'*64,'b'*64,'later','fixture'))
    latest = case_detail(conn,case_id=cid)['preview']
    assert '2025.01-k3' not in latest['plain_text'] and '尝试 MMC1' not in latest['plain_text']
    assert '本轮板卡操作回执' not in latest['plain_text']


def test_board_display_keeps_all_sections_in_one_snapshot(conn, monkeypatch):
    from k3_support import board_test_evidence as module
    observations = []
    def empty_items(*args, **kwargs):
        observations.append(conn.in_transaction)
        return []
    def empty_versions(*args, **kwargs):
        observations.append(conn.in_transaction)
        return {'observations':[], 'truncated':False,'unreadable_review_index':False}
    monkeypatch.setattr(module,'items',empty_items)
    monkeypatch.setattr(module,'reviewed_version_scan',empty_versions)
    assert not conn.in_transaction
    assert lines(conn,case_id='fixture',lifecycle_round=1)==[]
    assert observations==[True,True] and not conn.in_transaction


def test_retired_review_rejected_even_when_execution_hashes_remain_valid(conn, config):
    from k3_support.review_evidence import verified_board_output
    _case_reply(conn, config, 'board', serial_receipt=json.dumps(dict(ok=True,fresh=True,matched=True,
        rx_seq_start=1,output='U-Boot 2025.01\n')))
    review = conn.execute('SELECT * FROM codex_reviews ORDER BY rowid DESC LIMIT 1').fetchone()
    candidate = None
    for evidence in conn.execute('SELECT * FROM evidence WHERE case_id=?', (review['case_id'],)):
        receipt = verified_board_output(conn, review=review, item=evidence)
        if receipt is not None and receipt['action'].get('type') == 'serial_wait':
            candidate = evidence
            break
    assert candidate is not None
    round_number = conn.execute('SELECT lifecycle_round FROM jobs WHERE job_id=?', (review['job_id'],)).fetchone()[0]
    conn.execute('INSERT INTO case_content_retirements VALUES(?,?,?,?,?,?,?)',
                 (review['case_id'],round_number,'review-retired','a'*64,'b'*64,'now','fixture'))
    before = conn.serialize()
    assert verified_board_output(conn, review=review, item=candidate) is None
    assert conn.serialize() == before


@pytest.mark.parametrize('index,warning', [(json.dumps(['missing'] * 101), '读取上限'), ('{}', '索引无法读取')])
def test_partial_version_scan_is_visible_even_without_observations(conn, config, index, warning):
    from k3_support.board_test_evidence import reviewed_version_scan
    _case_reply(conn, config, 'board')
    case = conn.execute('SELECT * FROM cases LIMIT 1').fetchone()
    conn.execute('UPDATE codex_reviews SET evidence_ids_json=?', (index,))
    before = conn.serialize()
    result = reviewed_version_scan(conn, case_id=case['case_id'], lifecycle_round=case['lifecycle_round'])
    assert result['observations'] == [] and not result['complete_history_verified']
    rendered = '\n'.join(lines(conn, case_id=case['case_id'], lifecycle_round=case['lifecycle_round']))
    assert warning in rendered
    assert conn.serialize() == before


@pytest.mark.parametrize('field', ['evidence_ids_json', 'independent_checks_json'])
def test_oversized_review_fields_are_not_decoded(conn, config, monkeypatch, field):
    from k3_support import board_test_evidence as module
    _case_reply(conn, config, 'board')
    case = conn.execute('SELECT * FROM cases LIMIT 1').fetchone()
    oversized = json.dumps(['PRIVATE' * 40000])
    conn.execute(f'UPDATE codex_reviews SET {field}=?', (oversized,))
    original = module.json.loads
    def bounded(value, *args, **kwargs):
        assert value != oversized, 'oversized review crossed the database read boundary'
        return original(value, *args, **kwargs)
    monkeypatch.setattr(module.json, 'loads', bounded)
    result = module.reviewed_version_scan(conn, case_id=case['case_id'], lifecycle_round=case['lifecycle_round'])
    assert result['unreadable_review_index'] and result['observations'] == []
    assert 'PRIVATE' not in str(result)


@pytest.mark.parametrize('invalidate', ['log', 'round'])
def test_complete_review_projects_versions_then_rejects_stale_receipt(conn, config, invalidate):
    receipt = json.dumps(dict(ok=True, fresh=True, matched=True, rx_seq_start=100,
                              output='PRIVATE SERIAL TEXT\nTrying to boot from MMC1\nU-Boot 2025.01-k3\n'))
    _case_reply(conn, config, 'board', serial_receipt=receipt)
    case = conn.execute('SELECT * FROM cases LIMIT 1').fetchone()
    before = conn.serialize()
    values = reviewed_versions(conn, case_id=case['case_id'], lifecycle_round=case['lifecycle_round'])
    assert len(values) == 1
    assert values[0]['component'] == 'u-boot' and values[0]['version'] == '2025.01-k3'
    assert values[0]['observed_at'] and values[0]['evidence_id'] and values[0]['session_id']
    rendered = '\n'.join(lines(conn, case_id=case['case_id'], lifecycle_round=case['lifecycle_round']))
    assert '2025.01-k3' in rendered and 'PRIVATE SERIAL TEXT' not in rendered
    assert '尝试 MMC1' in rendered and '不是成功启动介质' in rendered
    assert conn.serialize() == before
    if invalidate == 'log':
        conn.execute("UPDATE action_ledger SET result_json=json_set(result_json,'$.stdout','changed') WHERE action_type='board'")
        current_round = case['lifecycle_round']
    else:
        current_round = case['lifecycle_round']+1
        conn.execute('UPDATE cases SET lifecycle_round=? WHERE case_id=?', (current_round, case['case_id']))
    assert reviewed_versions(conn, case_id=case['case_id'], lifecycle_round=current_round) == []
    from k3_support.board_test_evidence import reviewed_version_scan
    assert reviewed_version_scan(conn, case_id=case['case_id'],
                                 lifecycle_round=current_round)['boot_attempts'] == []
