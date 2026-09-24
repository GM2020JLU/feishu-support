"""Real queue/database integration with synthetic read-only provider responses."""
# ruff: noqa: F811 -- imported pytest fixture
import pytest
from test_project_bug_search import QueryClient, context, run  # noqa: F401
from test_project_created_intake import created

from k3_support.project_bug_controls import execute


def start(conn, config):
    config.raw['project_integration']['search_spaces'][0]['allowed_item_ids']=None
    d=created(conn,state='draft')
    payload={k:d[k] for k in ('draft_id','expected_digest')}
    row=execute(conn,config,action='search-create-duplicates',payload=payload|{'keyword':'boot','request_id':'search-1'})
    return payload|{'search_id':row['search_id']}


def test_observed_candidates_are_bound_without_client_json(conn,config,context):
    payload=start(conn,config)
    with pytest.raises(ValueError):execute(conn,config,action='attach-create-search',payload=payload)
    run(conn,config)
    result=execute(conn,config,action='attach-create-search',payload=payload)
    assert [c['item_id'] for c in result['duplicate_candidates']]==['123','456']
    assert not result['duplicate_confirmed']
    with pytest.raises(ValueError,match='differ'):
        execute(conn,config,action='attach-create-duplicates',payload={k:payload[k] for k in ('draft_id','search_id')}|{'candidates':[]})


@pytest.mark.parametrize('bad',['scope','owner','full_page'])
def test_incomplete_or_foreign_results_cannot_satisfy_duplicate_check(conn,config,context,bad):
    payload=start(conn,config)
    run(conn,config,QueryClient(ids=tuple(range(1,51))) if bad=='full_page' else None)
    if bad=='scope':config.raw['project_integration']['search_spaces'][0]['allowed_item_ids']=[123]
    if bad=='owner':config.raw['identity']['control_operator_id']='other'
    with pytest.raises((ValueError,PermissionError)):
        execute(conn,config,action='attach-create-search',payload=payload)
    assert conn.execute('SELECT duplicate_search_id FROM project_bug_create_drafts').fetchone()[0] is None
