# ruff: noqa: F811 -- pytest injects the imported fixture
"""Dependent steps must be delivered through the real scoped worker bridge."""

from uuid import uuid4

import pytest
from test_project_verification_runs import context  # noqa: F401
from test_verification_reviews import execute_fixture, payload
from test_verification_sources import observation
from test_verification_worker_delivery import worker

from k3_support import project_verification as plans
from k3_support import project_verification_reviews as reviews
from k3_support import project_verification_runs as runs
from k3_support.broker_remote_runner import run_one


def prepare_child(conn, context):
    ctx, parent = execute_fixture(conn, context, depends=True)
    reviews.record(conn, actor='owner', payload=payload(conn, parent))
    intent = ctx[-2] | {'step_id':'dependent', 'request_id':'dependent', 'remote_request_id':str(uuid4())}
    child = runs.prepare(conn, **intent)
    call, _, _ = worker(conn, ctx)
    return ctx, parent, child, call


def listed(conn, call, child):
    before = list(conn.iterdump())
    cursor = ''
    found = None
    while True:
        page = call(operation='verification_list', request_id=str(uuid4()), after_id=cursor)
        for item in page['items']:
            if item['run_id'] == child['run_id']:
                found = item
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert list(conn.iterdump()) == before
    assert found is not None
    return found


def test_reviewed_dependency_discovered_submitted_and_executed_by_worker(conn, context):
    ctx, _, child, call = prepare_child(conn, context)
    item = listed(conn, call, child)
    assert item['dispatchable'] is True
    assert item['verification_state'] == 'not_run'
    accepted = call(operation='submit', request_id=item['remote_request_id'], **item['remote'])
    assert accepted['accepted'] is True
    assert listed(conn, call, child)['dispatchable'] is False
    def transport(**kwargs):
        kwargs['heartbeat']()
        return ({'exit_code':0, 'stdout':'dependent fixture', 'stderr':''}
                if kwargs.get('keepalive') else observation(conn, child['run_id']))
    result = run_one(conn, ctx[0], contract_reader=lambda:ctx[1], transport=transport)
    assert result['state'] == 'succeeded'
    delivered = listed(conn, call, child)
    assert delivered['execution_state'] == 'succeeded' and delivered['dispatchable'] is False
    assert delivered['verification_state'] == 'unknown'
    assert conn.execute('SELECT count(*) FROM project_bug_operations').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['review_failed','review_replaced','parent_rerun','plan_replaced','paused'])
def test_changed_dependency_is_visible_but_cannot_be_dispatched(conn, context, change):
    ctx, parent, child, call = prepare_child(conn, context)
    item = listed(conn, call, child)
    assert item['dispatchable'] is True
    if change in {'review_failed','review_replaced'}:
        reviews.record(conn, actor='owner', payload=payload(conn,parent,'failed' if change=='review_failed' else 'passed'))
    elif change == 'parent_rerun':
        runs.prepare(conn, **(ctx[-2] | {'request_id':'parent-rerun','remote_request_id':str(uuid4())}))
    elif change == 'plan_replaced':
        plans.publish(conn, bug_id=ctx[2]['bug_id'], round_id=ctx[3]['round_id'], actor='owner',
                      request_id='replacement', expected_revision=conn.execute('SELECT revision FROM project_bugs').fetchone()[0],
                      plan=ctx[4]['definition'])
    else:
        conn.execute("UPDATE project_bug_rounds SET execution_state='paused'")
    assert listed(conn, call, child)['dispatchable'] is False
    with pytest.raises(ValueError):
        call(operation='submit', request_id=item['remote_request_id'], **item['remote'])
    assert conn.execute('SELECT count(*) FROM broker_remote_actions WHERE request_id=?',(child['remote_request_id'],)).fetchone()[0] == 0
