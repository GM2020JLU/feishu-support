"""Creation member lookup tests: authority, minimization and dispatch identity."""
# ruff: noqa: F811
import pytest
from test_project_create_dispatch import SCOPE, cfg, conn, ready_draft  # noqa: F401
from test_project_create_form import Metadata

from k3_support import project_create_grants as grants
from k3_support.project_create_users import search, validate
from k3_support.project_read_client import ProjectReadError, _params


class Users(Metadata):
    def __init__(self, hook=None):
        super().__init__()
        self.user_hook=hook
    def read_page(self, command, params):
        if command!='user.search':return super().read_page(command,params)
        self.calls.append((command,params))
        if self.user_hook:self.user_hook()
        return {'host':SCOPE['host'],'command':command,'payload':[
            {'user_key':'member','name_cn':'同名人员','status':'activated','email':'PRIVATE_EMAIL'}]}


def test_lookup_scoped_minimized_and_revocation_fenced(conn,cfg):
    draft=ready_draft(conn,cfg)
    params=dict(actor='owner',grant_id=draft['grant_id'],**SCOPE,field_key='owner',query='member')
    client=Users()
    result=search(conn,cfg,**params,client_factory=lambda _:client)
    assert result=={'options':[{'value':'member','label':'同名人员'}]}
    assert client.calls[-1]==('user.search',{'project_key':'space','user_keys':['member']})
    with pytest.raises(ValueError,match='field'):
        search(conn,cfg,**(params|{'field_key':'name'}),client_factory=lambda _:Users())
    client=Users(lambda:grants.revoke(conn,actor='owner',grant_id=draft['grant_id']))
    with pytest.raises(PermissionError):search(conn,cfg,**params,client_factory=lambda _:client)


def test_dispatch_rejects_names_duplicates_and_unresolved_members():
    metadata={'FieldConfList':[{'field_key':'owner','field_type_key':'multi_user'}]}
    validate(Users(),SCOPE,metadata,{'owner':['member']})
    for value in ['member',['member','member'],['name'],[42],{}]:
        with pytest.raises(ValueError):validate(Users(),SCOPE,metadata,{'owner':value})


def test_read_boundary_requires_space_and_bounded_active_lookup():
    _params('user.search',{'project_key':'space','user_keys':['member']})
    for params in [{'user_keys':['member']},{'project_key':'space','user_keys':['x']*21},
                   {'project_key':'space','user_keys':['member'],'need_all_status':True}]:
        with pytest.raises(ProjectReadError):_params('user.search',params)


def test_single_member_is_scalar_and_rejects_array():
    metadata={'FieldConfList':[{'field_key':'owner','field_type_key':'user'}]}
    validate(Users(),SCOPE,metadata,{'owner':'member'})
    with pytest.raises(ValueError):validate(Users(),SCOPE,metadata,{'owner':['member']})
