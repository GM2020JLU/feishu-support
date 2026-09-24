import copy

import pytest

from k3_support.scope_facts import analyze_scope_facts
from k3_support.context_facts import project_facts
from k3_support.retention_fact_fields import fact_fields, fact_json_fields
from k3_support.retention_fact_fields import redact_fact_content


def fixture(context):
    query = '当前 U-Boot 版本是 private-2025，当前使用 UFS，不是 NVMe'
    if not context:
        return analyze_scope_facts(query, supplied={'software_version':'private-2025'})
    return project_facts([dict(content=query,sender_id='private-author',role='colleague',
        order=1,event_pk='private-event',external_id='private-message',event_digest='source-digest')],
        query=query,requester_id='private-author')


@pytest.mark.parametrize('context',[False,True])
def test_actual_fact_producers_are_classified_without_exposing_values(context):
    value = fixture(context)
    before = copy.deepcopy(value)
    result = fact_fields(value)
    assert result['shape_recognized'] and not result['clear_allowed']
    total = 0
    for path in result['body_paths']:
        item = value
        for part in path.split('/')[1:]:
            item = item[int(part)] if isinstance(item,list) else item[part]
        total += len(item.encode())
    assert result['body_bytes'] == total and total > 0
    assert any('/text' in path for path in result['body_paths'])
    assert any('/observed_scope/' in path for path in result['body_paths'])
    assert 'private-' not in str(result)
    assert value == before


@pytest.mark.parametrize('path',[(),('mentions',0),('mentions',0,'source'),('fields','software_version')])
def test_unknown_nested_fields_do_not_inherit_classification(path):
    value = fixture(True)
    target = value
    for part in path:
        target = target[part]
    target['new_private_field'] = 'private-new'
    result = fact_fields(value)
    assert not result['shape_recognized'] and not result['clear_allowed']
    assert result['body_paths'] == [] and result['body_bytes'] == 0
    assert 'private-new' not in str(result)


@pytest.mark.parametrize('change',['policy','index','nested','size'])
def test_old_or_invalid_fact_shapes_are_not_partially_classified(change):
    value = fixture(True)
    if change=='policy':
        value['policy']='conversation-facts-v1'
    elif change=='index':
        value['fields']['software_version']['current_mention_indexes']=[True]
    elif change=='nested':
        value['mentions'][0]['subject']={'text':'private'}
    else:
        value['mentions'][0]['text']='x'*262145
    assert not fact_fields(value)['shape_recognized']


@pytest.mark.parametrize('raw',['{"policy":"a","policy":"b"}', 'NaN', '['*1500+'0'+']'*1500, None],
                         ids=['duplicate','nan','deep','missing'])
def test_json_adapter_fails_closed(raw):
    result = fact_json_fields(raw)
    assert not result['shape_recognized'] and not result['clear_allowed']


def test_case_inventory_counts_current_producer_without_disclosure(conn, config):
    from test_conversation_context import admitted,case,item
    from k3_support.conversation_context import bind_context_case
    from k3_support.case_content_inventory import preview
    key,snapshot = admitted(conn,config,item(content='当前 U-Boot 版本是 private-version'))
    cid = case(conn,key)
    bind_context_case(conn,snapshot['context_id'],cid)
    before = conn.serialize()
    result = preview(conn,cid)
    fields = result['context_fact_fields']
    assert fields['recognized_rows']==1 and fields['unclassified_rows']==0
    assert fields['body_bytes']>0 and not fields['clear_allowed']
    assert fields['candidate_transform_rows']==1 and fields['candidate_removed_bytes']==fields['body_bytes']
    assert 'private-version' not in str(result) and conn.serialize()==before
    conn.execute("UPDATE conversation_contexts SET facts_json=json_set(facts_json,'$.unknown','private') WHERE case_id=?",(cid,))
    fields = preview(conn,cid)['context_fact_fields']
    assert fields['unclassified_rows']==1 and fields['recognized_rows']==0 and fields['body_bytes']==0
    assert fields['candidate_transform_rows']==0 and fields['candidate_removed_bytes']==0


@pytest.mark.parametrize('context',[False,True])
def test_candidate_is_a_tombstone_preserving_sources_not_live_facts(context):
    from k3_support.ids import digest
    value = fixture(context)
    before = copy.deepcopy(value)
    classified = fact_fields(value)
    result = redact_fact_content(value,expected_digest=classified['input_digest'])
    assert result['value']['content_state']=='retired' and not result['clear_allowed']
    assert result['output_digest']==digest(result['value'])
    assert result['removed_body_bytes']==classified['body_bytes']
    retained = result['value']['retained_metadata']
    assert [m['source'] for m in retained['mentions']] == [m['source'] for m in value['mentions']]
    assert retained['query_digest']==value['query_digest']
    for path in classified['body_paths']:
        item = retained
        for part in path.split('/')[1:]:
            item = item[int(part)] if isinstance(item,list) else item[part]
        assert item is None
    assert 'private-2025' not in str(result)
    assert not fact_fields(result['value'])['shape_recognized']
    assert value==before
    with pytest.raises(ValueError,match='changed'):
        redact_fact_content(value,expected_digest='0'*64)
    with pytest.raises(ValueError,match='unclassified'):
        redact_fact_content(result['value'],expected_digest=result['output_digest'])
