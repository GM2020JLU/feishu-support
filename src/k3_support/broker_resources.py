"""Control-owned resource settlement, independent of business-result acceptance.

Callers hold the write transaction. No network, board I/O, or inferred exits.
The snapshot is not an authorization credential; the broker database remains
behind its existing independent control-side permission boundary.
"""

import json
from dataclasses import dataclass
from datetime import datetime

from .broker_input import project
from .broker_remote_state import UNSETTLED
from .timeutil import iso_now


@dataclass(frozen=True)
class Settlement:
    state: str
    reason: str | None = None
    evidence_ids: tuple[str, ...] = ()


def capture(conn, *, grant_id):
    """Capture once, or conservatively backfill from an exact retained input."""
    if not conn.in_transaction:
        raise ValueError('resource binding requires control transaction')
    row = conn.execute('SELECT * FROM broker_execution_resources WHERE grant_id=?', (grant_id,)).fetchone()
    if row is not None:
        return row
    grant = conn.execute('''SELECT g.*,s.authorized_at FROM broker_grants g
        JOIN broker_execution_starts s USING(grant_id)
        WHERE g.grant_id=? AND s.job_id=g.job_id AND s.attempt_no=g.attempt_no
          AND s.peer_uid=g.worker_uid''', (grant_id,)).fetchone()
    if grant is None:
        raise ValueError('resource start binding unavailable')
    claims = conn.execute('''SELECT request_id,binding_json FROM broker_claim_receipts
        WHERE peer_uid=? AND json_extract(binding_json,'$.job_id')=?
          AND json_extract(binding_json,'$.execution_round')=?''',
        (grant['worker_uid'], grant['job_id'], grant['attempt_no'])).fetchall()
    if len(claims) != 1:
        raise ValueError('resource claim binding unavailable')
    claim = json.loads(claims[0]['binding_json'])
    if (not isinstance(claim, dict) or not isinstance(claim.get('case_id'), str)
            or any(claim.get(key) != grant[column] for key, column in (
                ('job_id', 'job_id'), ('execution_round', 'attempt_no'),
                ('lifecycle_round', 'lifecycle_round'), ('input_digest', 'input_digest')))):
        raise ValueError('resource claim binding changed')
    inputs = project(conn, job_id=grant['job_id'], case_id=claim['case_id'],
                     lifecycle_round=grant['lifecycle_round'], input_digest=grant['input_digest'],
                     request_id=claims[0]['request_id'])
    session = inputs.get('board_session_id')
    # Existing action evidence must agree before historical material is accepted.
    sessions = {item[0] for item in conn.execute('''SELECT session_id FROM broker_board_actions WHERE grant_id=?
        UNION SELECT session_id FROM broker_board_cleanup WHERE grant_id=?''', (grant_id, grant_id))}
    if sessions and sessions != {session}:
        raise ValueError('resource board evidence conflicts with input')
    conn.execute('''INSERT INTO broker_execution_resources
        (grant_id,claim_request_id,job_id,attempt_no,lifecycle_round,input_digest,case_id,
         board_session_id,board_not_required,captured_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)''',
        (grant_id, claims[0]['request_id'], grant['job_id'], grant['attempt_no'],
         grant['lifecycle_round'], grant['input_digest'], claim['case_id'], session, int(session is None), iso_now()))
    return conn.execute('SELECT * FROM broker_execution_resources WHERE grant_id=?', (grant_id,)).fetchone()


def exited_binding(conn, *, grant_id):
    resource = capture(conn, grant_id=grant_id)
    exact = conn.execute('''SELECT i.invocation_id,s.authorized_at FROM broker_execution_instances i
        JOIN broker_service_exits e ON e.grant_id=i.grant_id AND e.invocation_id=i.invocation_id
        JOIN broker_execution_starts s ON s.grant_id=i.grant_id
        JOIN broker_grants g ON g.grant_id=s.grant_id
        WHERE i.grant_id=? AND i.claim_request_id=? AND s.job_id=? AND s.attempt_no=?
          AND g.job_id=s.job_id AND g.attempt_no=s.attempt_no
          AND g.lifecycle_round=? AND g.input_digest=?''',
        (grant_id, resource['claim_request_id'], resource['job_id'], resource['attempt_no'],
         resource['lifecycle_round'], resource['input_digest'])).fetchone()
    if exact is None:
        raise ValueError('exact resource exit unavailable')
    return {**dict(resource), **dict(exact)}


def require_open(conn, *, grant_id):
    if not conn.in_transaction:
        raise ValueError('resource fence requires control transaction')
    if conn.execute('''SELECT 1 FROM broker_execution_resources r
        LEFT JOIN broker_launches l USING(claim_request_id)
        WHERE r.grant_id=? AND (r.settled_at IS NOT NULL OR l.state='finished')''', (grant_id,)).fetchone():
        raise ValueError('execution resources already settled')


def cancel_unstarted(conn, *, grant_id):
    """An exit only cancels queue entries without any dispatched/result evidence."""
    for kind in ('remote', 'board'):
        conn.execute(f'''UPDATE broker_{kind}_actions SET state='cancelled',updated_at=?
            WHERE grant_id=? AND state='queued' AND NOT EXISTS
              (SELECT 1 FROM broker_{kind}_results r WHERE r.request_id=broker_{kind}_actions.request_id)''',
            (iso_now(), grant_id))


def inspect_settlement(conn, *, grant_id):
    if not conn.in_transaction:
        raise ValueError('settlement requires control transaction')
    try:
        bound = exited_binding(conn, grant_id=grant_id)
    except (ValueError, TypeError, KeyError):
        return Settlement('unverified', 'resource_binding_unverified')
    cancel_unstarted(conn, grant_id=grant_id)
    if conn.execute('SELECT 1 FROM broker_remote_actions a LEFT JOIN broker_remote_results r USING(request_id) '
                    f'WHERE a.grant_id=? AND {UNSETTLED} LIMIT 1', (grant_id,)).fetchone():
        return Settlement('pending', 'remote_cleanup_required')
    cleanup = conn.execute('SELECT session_id,state FROM broker_board_cleanup WHERE grant_id=?', (grant_id,)).fetchone()
    operations = conn.execute('''SELECT a.request_id,a.session_id,a.state,a.updated_at,r.exit_code,r.received_at
        FROM broker_board_actions a LEFT JOIN broker_board_results r USING(request_id) WHERE a.grant_id=?''',
        (grant_id,)).fetchall()
    session = bound['board_session_id']
    if cleanup is not None and (cleanup['state'] != 'succeeded' or cleanup['session_id'] != session):
        return Settlement('pending', 'board_cleanup_required')
    active = [op for op in operations if not (op['state'] == 'cancelled' and op['received_at'] is None)]
    if session is None:
        if active:
            return Settlement('unverified', 'board_cleanup_required')
        return Settlement('settled', evidence_ids=(bound['invocation_id'],))
    from .review import ReviewError, _verified_board_cleanup
    try:
        checked = _verified_board_cleanup(conn, case_id=bound['case_id'], session_id=session, require_unoccupied=False)
        started = datetime.fromisoformat(bound['authorized_at'])
        finished = [datetime.fromisoformat(item['finished_at']) for item in checked]
        if started.tzinfo is None or not finished or any(t.tzinfo is None or t < started for t in finished):
            raise ValueError('cleanup predates execution')
        for op in active:
            code = op['exit_code']
            if (op['session_id'] != session or type(code) is not int
                    or not ((op['state'] == 'succeeded' and code == 0)
                            or (op['state'] == 'failed' and 0 < code < 255 and code not in (124, 125)))):
                raise ValueError('board operation has no settled receipt')
            for stamp in (op['updated_at'], op['received_at']):
                ended = datetime.fromisoformat(stamp)
                if ended.tzinfo is None or any(t < ended for t in finished):
                    raise ValueError('board operation supersedes cleanup')
    except (ReviewError, ValueError, TypeError, KeyError):
        return Settlement('pending', 'board_cleanup_required')
    return Settlement('settled', evidence_ids=(bound['invocation_id'], *(op['request_id'] for op in active)))
