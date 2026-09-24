"""Relation selection must not turn creation grants into arbitrary-space reads."""
# ruff: noqa: F811
import pytest
from test_project_create_dispatch import SCOPE, cfg, conn, ready_draft  # noqa: F401

from k3_support.project_create_related import search, validate

FIELD={'field_key':'version','field_type_key':'work_item_related_multi_select','field_name':'发现版本','is_required':1}
TARGET={'project_key':'space','work_item_type':'version'}
READ_SCOPE={'simple_name':'space','project_key':'space','type_key':'version','allowed_item_ids':[100,200]}


class Related:
    def __init__(self, hook=None, missing=False):
        self.queries=[]
        self.hook=hook
        self.missing=missing
    def read_page(self,command,params):
        if command=='workitem.meta-create-fields':payload={'FieldConfList':[FIELD]}
        else:
            payload={'list':[{'field_key':'version','field_type':'workitem_related_multi_select',
                               'related_work_item_info':[TARGET]}],
                     'pagination':{'page_num':1,'page_size':50,'has_more':False,'total':1}}
        return {'host':SCOPE['host'],'command':command,'payload':payload}
    def query_bugs(self,scope,**kwargs):
        self.queries.append((scope.copy(),kwargs))
        if self.hook:self.hook()
        ids=[] if self.missing else scope['allowed_item_ids']
        return {'host':SCOPE['host'],'items':[{'item_id':str(i),'title':'Version '+str(i)} for i in ids],
                'next_after_id':None}


def test_related_search_needs_target_scope_and_rechecks_it(conn,cfg):
    draft=ready_draft(conn,cfg)
    args=dict(actor='owner',grant_id=draft['grant_id'],**SCOPE,field_key='version',query='V1')
    client=Related()
    with pytest.raises(PermissionError,match='read scope'):
        search(conn,cfg,**args,client_factory=lambda _:client)
    assert client.queries==[]
    cfg.raw['project_integration']['search_spaces'].append(READ_SCOPE.copy())
    result=search(conn,cfg,**args,client_factory=lambda _:client)
    assert result['options']==[{'value':'100','label':'Version 100'},{'value':'200','label':'Version 200'}]
    assert client.queries[-1]==(READ_SCOPE,{'keyword':'V1'})
    client=Related(hook=lambda:cfg.raw['project_integration']['search_spaces'].pop())
    with pytest.raises(PermissionError):search(conn,cfg,**args,client_factory=lambda _:client)


def test_related_dispatch_reads_only_selected_authorized_ids_and_fences_changes(cfg):
    cfg.raw['project_integration']['search_spaces'].append(READ_SCOPE.copy())
    client=Related()
    recheck=validate(client,cfg,SCOPE,{'FieldConfList':[FIELD]},{'version':[100]})
    assert client.queries==[(READ_SCOPE|{'allowed_item_ids':[100]}, {})]
    recheck()
    cfg.raw['project_integration']['search_spaces'][-1]['allowed_item_ids']=[200]
    with pytest.raises(PermissionError):recheck()


def test_related_dispatch_rejects_wrong_types_outside_scope_and_missing_ids(cfg):
    cfg.raw['project_integration']['search_spaces'].append(READ_SCOPE.copy())
    for value in ['100',['100'],[True],[100,100],[2**53]]:
        with pytest.raises(ValueError):validate(Related(),cfg,SCOPE,{'FieldConfList':[FIELD]},{'version':value})
    client=Related()
    with pytest.raises(PermissionError):validate(client,cfg,SCOPE,{'FieldConfList':[FIELD]},{'version':[300]})
    assert not client.queries
    with pytest.raises(ValueError,match='resolve'):
        validate(Related(missing=True),cfg,SCOPE,{'FieldConfList':[FIELD]},{'version':[100]})


def test_search_detects_in_place_scope_change(conn,cfg):
    draft=ready_draft(conn,cfg)
    cfg.raw['project_integration']['search_spaces'].append(dict(READ_SCOPE,allowed_item_ids=[100,200]))
    client=Related(hook=lambda:cfg.raw['project_integration']['search_spaces'][-1]['allowed_item_ids'].remove(100))
    with pytest.raises(PermissionError):
        search(conn,cfg,actor='owner',grant_id=draft['grant_id'],**SCOPE,field_key='version',query='V1',client_factory=lambda _:client)
