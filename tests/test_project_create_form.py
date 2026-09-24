"""Scope-controlled official metadata projection, with no mutation transport."""
# ruff: noqa: F811 -- imported fixtures
import pytest
from test_project_create_dispatch import SCOPE, cfg, conn, ready_draft  # noqa: F401

from k3_support import project_create_grants as grants
from k3_support.project_bug_controls import execute
from k3_support.project_create_schema import form


class Metadata:
    def __init__(self, hook=None):
        self.calls=[]
        self.hook=hook
    def read_page(self,command,params):
        self.calls.append((command,params))
        if self.hook:self.hook()
        return {'host':SCOPE['host'],'command':command,'payload':{'FieldConfList':[
            {'field_key':'name','field_name':'标题','field_type_key':'text','is_required':1},
            {'field_key':'owner','field_name':'经办人','field_type_key':'multi_user','is_required':0,
             'default_value':{'value':'PRIVATE_DEFAULT_MUST_NOT_BE_EXPOSED'}}]}}


def test_create_scope_options_exposes_only_configured_safe_identifiers(conn, cfg):
    cfg.raw["project_integration"]["intake_spaces"] = [
        {
            "simple_name": "k3",
            "project_key": "space",
            "type_keys": ["issue", "bug"],
        }
    ]

    result = execute(conn, cfg, action="create-scope-options", payload={})

    assert result == {
        "available": True,
        "reader_host": "project.feishu.cn",
        "options": [
            {"simple_name": "k3", "project_key": "space", "type_key": "issue"},
            {"simple_name": "k3", "project_key": "space", "type_key": "bug"},
        ],
    }
    assert "executable" not in str(result)
    assert "sha256" not in str(result)


def test_form_projects_labels_without_interpreting_defaults(conn,cfg):
    draft=ready_draft(conn,cfg)
    client=Metadata()
    result=form(conn,cfg,actor='owner',grant_id=draft['grant_id'],**SCOPE,client_factory=lambda _:client)
    assert result['fields']==[
        {'field_key':'name','label':'标题','type':'text','required':True,'editor':'text'},
        {'field_key':'owner','label':'经办人','type':'multi_user','required':False,'editor':'user'}]
    assert 'PRIVATE_DEFAULT' not in str(result)
    assert client.calls==[('workitem.meta-create-fields',{'project_key':'space','work_item_type':'bug'})]


@pytest.mark.parametrize('stage',['before','during'])
def test_revoked_grant_never_returns_metadata(conn,cfg,stage):
    draft=ready_draft(conn,cfg)
    revoke=lambda:grants.revoke(conn,actor='owner',grant_id=draft['grant_id'])
    if stage=='before':revoke()
    client=Metadata(hook=revoke if stage=='during' else None)
    with pytest.raises(PermissionError):
        form(conn,cfg,actor='owner',grant_id=draft['grant_id'],**SCOPE,client_factory=lambda _:client)
    assert len(client.calls)==(0 if stage=='before' else 1)


class SelectMetadata(Metadata):
    def __init__(self, choices=None):
        super().__init__()
        self.choices=choices if choices is not None else [{'option_id':'0','option_name':'P0'},{'option_id':'99','option_name':'待定'}]
    def read_page(self,command,params):
        self.calls.append((command,params))
        if command=='workitem.meta-create-fields':
            data={'FieldConfList':[{'field_key':'priority','field_name':'优先级','field_type_key':'select','is_required':1}]}
        else:
            assert params['field_keys']==['priority']
            data={'list':[{'field_key':'priority','field_type':'select','option':self.choices}],
                  'pagination':{'page_num':1,'page_size':50,'has_more':False,'total':1}}
        return {'host':SCOPE['host'],'command':command,'payload':data}


def test_flat_select_form_and_submission_use_current_option_ids(conn,cfg):
    from k3_support.project_create_schema import check
    draft=ready_draft(conn,cfg)
    result=form(conn,cfg,actor='owner',grant_id=draft['grant_id'],**SCOPE,client_factory=lambda _:SelectMetadata())
    assert result['fields'][0]['editor']=='select'
    assert result['fields'][0]['options']==[{'value':'0','label':'P0'},{'value':'99','label':'待定'}]
    check(SelectMetadata(),SCOPE,{'priority':'0'})
    for invalid in ['3','P0',0,{'value':'0'}]:
        with pytest.raises(ValueError,match='option'):check(SelectMetadata(),SCOPE,{'priority':invalid})


def test_malformed_or_duplicate_options_do_not_become_selectable(conn,cfg):
    from k3_support.project_create_schema import check
    from k3_support.project_read_client import ProjectReadError
    for choices in [[{'option_id':'0','option_name':'P0'}]*2,[{'option_id':'x','option_name':'X','children':[]}]]:
        with pytest.raises(ProjectReadError):check(SelectMetadata(choices),SCOPE,{'priority':'0'})


class TreeMetadata(SelectMetadata):
    def read_page(self, command, params):
        result=super().read_page(command,params)
        if command=='workitem.meta-create-fields':
            result['payload']['FieldConfList'][0]['field_type_key']='tree_select'
        else:
            result['payload']['list'][0]['field_type']='tree-select'
        return result


def test_tree_form_displays_paths_but_only_submits_leaf_ids(conn,cfg):
    from k3_support.project_create_schema import check
    options=[{'option_id':'parent','option_name':'Platform','children':[
        {'option_id':'leaf','option_name':'Boot'}]}]
    draft=ready_draft(conn,cfg)
    result=form(conn,cfg,actor='owner',grant_id=draft['grant_id'],**SCOPE,client_factory=lambda _:TreeMetadata(options))
    assert result['fields'][0]['options']==[{'value':'leaf','label':'Platform / Boot'}]
    check(TreeMetadata(options),SCOPE,{'priority':'leaf'})
    for value in ['parent','Platform / Boot']:
        with pytest.raises(ValueError,match='option'):
            check(TreeMetadata(options),SCOPE,{'priority':value})


def test_tree_malformed_children_and_ambiguous_ids_are_rejected():
    from k3_support.project_create_schema import _choices
    from k3_support.project_read_client import ProjectReadError
    for options in [
        [{'option_id':'x','option_name':'X','children':None}],
        [{'option_id':'x','option_name':'X','children':[{'option_id':'x','option_name':'Duplicate'}]}],
    ]:
        with pytest.raises(ProjectReadError):_choices(options,tree=True)
