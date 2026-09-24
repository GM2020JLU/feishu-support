"""Both authenticated chat channels submit into actual existing job/control tables."""
import json
import shlex

import pytest
from test_project_chat_control import message
from test_project_investigation import setup

from k3_support.approvals import ApprovalError
from k3_support.control import ControlMessage, execute_control
from k3_support.ids import digest


def command(payload, **changes):
    values = {'bug':payload['bug_id'], 'round':payload['round_id'], 'repo':payload['repository'],
              'tool':payload['executor_id'], 'branch':'main', 'commit':'a'*40,
              'task':'Find and repair the failing software behavior',
              'acceptance':'Original regression fails at baseline and passes after repair'} | changes
    return shlex.join(['bug','investigate', *values.values()])


@pytest.mark.parametrize('channel', ['telegram','feishu'])
def test_chat_creates_bound_job_replays_after_pause_and_rejects_changed_message(conn,config,tmp_path,channel):
    cfg,payload=setup(conn,config,tmp_path)
    msg=message(cfg,command(payload),channel)
    result=execute_control(conn,cfg,msg,control_channel=channel)
    assert '已创建' in result['text']
    row=conn.execute('SELECT * FROM jobs').fetchone()
    context=json.loads(row['context_json'])
    inputs=json.loads(conn.execute('SELECT payload_json FROM broker_inputs').fetchone()[0])
    assert digest(inputs)==row['input_digest']
    assert inputs['context_extra']['operator_request']==context['operator_request']
    assert context['operator_request']['origin']=={'channel':channel,'intent_digest':digest(shlex.split(msg.text))}
    assert context['project_investigation']['bug_id']==payload['bug_id']
    assert context['project_investigation']['source']['base_commit']=='a'*40
    assert context['repositories']==[payload['repository']]
    conn.execute("UPDATE cases SET state='paused',version=version+1")
    replay=execute_control(conn,cfg,msg,control_channel=channel)
    assert row['job_id'] in replay['text'] and '未重复创建' in replay['text']
    changed=ControlMessage(msg.user_id,msg.chat_id,msg.message_id,command(payload,task='different task'))
    with pytest.raises(ValueError,match='native message ID.*different'):
        execute_control(conn,cfg,changed,control_channel=channel)
    with pytest.raises(ValueError,match='resume|permit'):
        execute_control(conn,cfg,message(cfg,command(payload),channel,'new-after-pause'),control_channel=channel)
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM project_bug_operations').fetchone()[0]==0


@pytest.mark.parametrize('channel', ['telegram','feishu'])
@pytest.mark.parametrize('bad', ['identity','repository','sha','tool'])
def test_chat_bad_identity_or_selection_creates_no_job(conn,config,tmp_path,channel,bad):
    cfg,payload=setup(conn,config,tmp_path)
    changes={'repository':{'repo':'not-configured'},'sha':{'commit':'abc'},'tool':{'tool':'not-installed'}}.get(bad,{})
    msg=message(cfg,command(payload,**changes),channel)
    if bad=='identity':msg=ControlMessage('intruder',msg.chat_id,msg.message_id,msg.text)
    with pytest.raises((ApprovalError,ValueError,RuntimeError)):
        execute_control(conn,cfg,msg,control_channel=channel)
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==0


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_chat_options_do_not_start_round_or_job(conn,config,tmp_path,channel):
    cfg,payload=setup(conn,config,tmp_path)
    result=execute_control(conn,cfg,message(cfg,'bug coding-options '+payload['bug_id'],channel),control_channel=channel)
    assert payload['repository'] in result['text'] and payload['executor_id'] in result['text']
    assert '完整提交 SHA' in result['text']
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
    assert conn.execute('SELECT count(*) FROM project_bug_rounds').fetchone()[0]==1


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_chat_initial_round_then_task_without_gui(conn,config,tmp_path,channel):
    from test_coding_tasks import request_fixture
    from test_project_bugs import binding
    cfg, request=request_fixture(conn,config,tmp_path)
    bug=binding(conn,request['case_id'])
    msg=message(cfg,shlex.join(['bug','start-round',bug['bug_id'],str(bug['revision']),'Investigate reported software failure']),channel,'new-round')
    first=execute_control(conn,cfg,msg,control_channel=channel)
    assert execute_control(conn,cfg,msg,control_channel=channel)==first
    payload={**request,'bug_id':bug['bug_id'],
             'round_id':conn.execute('SELECT round_id FROM project_bug_rounds').fetchone()[0]}
    created=execute_control(conn,cfg,message(cfg,command(payload),channel,'new-task'),control_channel=channel)
    assert '已创建' in created['text']
    assert conn.execute('SELECT count(*) FROM project_bug_rounds').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM project_investigation_jobs').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM project_bug_operations').fetchone()[0]==0
    stale=message(cfg,shlex.join(['bug','start-round',bug['bug_id'],str(bug['revision']),'another round']),channel,'stale-round')
    with pytest.raises(ValueError):execute_control(conn,cfg,stale,control_channel=channel)
    assert conn.execute('SELECT count(*) FROM project_bug_rounds').fetchone()[0]==1


def test_delayed_chat_intent_cannot_move_to_a_new_investigation_round(conn,config,tmp_path):
    cfg,payload=setup(conn,config,tmp_path)
    text=command(payload, round='previous-round-id')
    with pytest.raises(ValueError,match='调查轮次'):
        execute_control(conn,cfg,message(cfg,text),control_channel='telegram')
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==0


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_chat_can_continue_settled_round_in_second_repository(conn,config,tmp_path,channel):
    from test_project_repair_reviews import setup_evidence

    cfg,task,first_job,_case=setup_evidence(conn,config,tmp_path)
    cfg.raw['repositories']['linux']=dict(cfg.raw['repositories']['u-boot'])
    second=command(task,repo='linux',commit='c'*40)+' '+shlex.quote(first_job)
    msg=message(cfg,second,channel,'second-repository')
    created=execute_control(conn,cfg,msg,control_channel=channel)
    assert '已创建' in created['text']
    row=conn.execute('SELECT * FROM jobs WHERE job_id!=?',(first_job,)).fetchone()
    context=json.loads(row['context_json'])['project_investigation']
    assert context['round_id']==task['round_id']
    assert context['predecessor_job_id']==first_job
    assert context['source']['base_commit']=='c'*40
    assert json.loads(row['context_json'])['repositories']==['linux']
    assert '未重复创建' in execute_control(conn,cfg,msg,control_channel=channel)['text']
    status=execute_control(conn,cfg,message(cfg,'bug '+task['bug_id'],channel,'multi-repo-status'),control_channel=channel)['text']
    assert f"{row['job_id']} · 回合 {task['round_id']} · linux" in status
    assert f'接续 {first_job}' in status
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==2


def test_chat_rejects_unsettled_predecessor(conn,config,tmp_path):
    cfg,payload=setup(conn,config,tmp_path)
    first=execute_control(conn,cfg,message(cfg,command(payload),'telegram','first'),control_channel='telegram')
    first_job=first['text'].split('调查任务：',1)[1].splitlines()[0]
    continued=command(payload)+' '+shlex.quote(first_job)
    with pytest.raises(ValueError,match='predecessor|settle'):
        execute_control(conn,cfg,message(cfg,continued,'telegram','unsettled'),control_channel='telegram')
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==1


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_gateway_submits_chat_investigation_through_real_control_cli(conn,config,tmp_path,monkeypatch,channel):
    import asyncio
    from types import SimpleNamespace

    import yaml
    from test_hermes_control_plugin import FakeAdapter, wait_for_receipts, write_runtime

    from k3_support.hermes_plugin import pre_gateway_dispatch

    cfg,payload=setup(conn,config,tmp_path)
    msg=message(cfg,command(payload),channel)
    cfg.path.write_text(yaml.safe_dump(cfg.raw))
    runtime=write_runtime(tmp_path,cfg.path)
    monkeypatch.setenv('K3_SUPPORT_CONTROL_PLUGIN_CONFIG',str(runtime))

    async def exercise():
        adapter=FakeAdapter()
        gateway=SimpleNamespace(adapters={channel:adapter})
        event=SimpleNamespace(text=msg.text,message_id=msg.message_id,raw_message=None,
            source=SimpleNamespace(platform=channel,user_id=msg.user_id,chat_id=msg.chat_id))
        assert pre_gateway_dispatch(event,gateway)['action']=='skip'
        await wait_for_receipts(adapter)
        assert '已创建' in adapter.sent[0][1]
    asyncio.run(exercise())
    jobs=conn.execute('SELECT job_id FROM project_investigation_jobs WHERE round_id=?',(payload['round_id'],)).fetchall()
    assert len(jobs)==1
    assert conn.execute('SELECT count(*) FROM project_bug_operations').fetchone()[0]==0
