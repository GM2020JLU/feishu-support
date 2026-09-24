"""Real shared control/DB paths; no message delivery or Project calls."""
# ruff: noqa: F811
import pytest
from test_project_bugs import binding, observe
from test_project_link_intake import context  # noqa: F401

from k3_support.approvals import ApprovalError
from k3_support.control import ControlMessage, execute_control
from k3_support.hermes_plugin import _CONTROL_PREFIX, _format_receipt
from k3_support.project_bug_controls import execute
from k3_support.store import ConflictError, get_case


def message(config,text,channel='telegram',message_id='message-1'):
    config.raw['identity'].setdefault('control_operator_id','owner-user')
    if channel=='feishu':
        config.raw['identity'].update(feishu_control_user_id='native-feishu-user',feishu_control_chat_id='native-feishu-chat')
        return ControlMessage('native-feishu-user','native-feishu-chat',message_id,text)
    return ControlMessage(config.raw['identity']['telegram_control_user_id'],config.raw['identity']['telegram_control_chat_id'],message_id,text)


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_bug_chat_status_and_takeover_share_gui_case(conn,config,channel):
    bug=binding(conn);observe(conn,bug)
    result=execute_control(conn,config,message(config,'bug '+bug['bug_id'],channel),control_channel=channel)
    assert '远端状态（缓存）：actual-state-id' in result['text']
    assert '编码调查任务：尚未提交' in result['text']
    assert f"授权选项：bug grant-options {bug['bug_id']}" in result['text']
    assert '执行成功、修复完成、验证通过' in result['text']
    assert _format_receipt(True,result)==result['text']
    version=get_case(conn,bug['case_id'])['version']
    command=f"bug takeover {bug['bug_id']} {version} owner_requested"
    first=execute_control(conn,config,message(config,command,channel,'takeover-1'),control_channel=channel)
    assert first['state']=='takeover'
    assert execute(conn,config,action='detail',payload={'bug_id':bug['bug_id']})['case_id']==first['case_id']
    again=execute_control(conn,config,message(config,command,channel,'takeover-1'),control_channel=channel)
    assert again['version']==first['version']
    with pytest.raises(ConflictError):
        execute_control(conn,config,message(config,f"bug pause {bug['bug_id']} {version}",channel,'stale'),control_channel=channel)


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_bug_chat_status_shows_only_latest_three_investigation_jobs(conn,config,tmp_path,channel):
    from test_project_investigation import setup as investigation_setup
    from k3_support import project_bugs, project_investigation

    cfg,payload=investigation_setup(conn,config,tmp_path)
    for index in range(4):
        payload['request_id']=f"chat-progress-{index}"
        payload['expected_revision']=project_bugs.detail(conn,payload['bug_id'])['revision']
        project_investigation.submit(conn,cfg,payload)
    jobs=project_investigation.projection(conn,payload['bug_id'])
    conn.execute("UPDATE jobs SET state='succeeded' WHERE job_id=?",(jobs[0]['job_id'],))
    result=execute_control(conn,config,message(config,'bug '+payload['bug_id'],channel),control_channel=channel)

    assert '编码调查任务：已记录 4 个，显示最新 3 个' in result['text']
    assert 'Codex · 任务执行成功（不代表修复完成）' in result['text']
    for job in jobs[:3]:
        assert job['job_id'] in result['text']
    assert jobs[3]['job_id'] not in result['text']
    assert '接管：bug takeover' in result['text']


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_bug_chat_progress_reports_current_round_blockers_without_writing(conn,config,tmp_path,channel):
    from test_project_investigation import setup as investigation_setup
    from k3_support import project_bugs, project_investigation

    cfg,payload=investigation_setup(conn,config,tmp_path)
    payload['request_id']='chat-progress-read'
    payload['expected_revision']=project_bugs.detail(conn,payload['bug_id'])['revision']
    job=project_investigation.submit(conn,cfg,payload)
    before=conn.total_changes
    result=execute_control(conn,config,message(config,'bug progress '+payload['bug_id'],channel),control_channel=channel)
    assert conn.total_changes==before
    assert '远端状态（缓存）' in result['text']
    assert '资源待核对：' in result['text']
    assert job['job_id'] in result['text']
    assert '等待执行' in result['text']
    assert '未触发远端刷新' in result['text']
    assert f"完整详情和下一步：bug {payload['bug_id']}" in result['text']


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_import_is_scoped_and_deduplicates_native_message(conn,config,context,channel):
    msg=message(config,'bug import '+context['url'],channel)
    result=execute_control(conn,config,msg,control_channel=channel)
    assert execute_control(conn,config,msg,control_channel=channel)==result
    assert conn.execute('SELECT COUNT(*) FROM project_link_intakes').fetchone()[0]==1
    assert conn.execute('SELECT COUNT(*) FROM project_bug_operations').fetchone()[0]==0
    bad=ControlMessage('other',msg.chat_id,'foreign',msg.text)
    with pytest.raises(ApprovalError):execute_control(conn,config,bad,control_channel=channel)
    assert conn.execute('SELECT COUNT(*) FROM project_link_intakes').fetchone()[0]==1


def test_bug_prefix_never_falls_through_to_model():
    assert _CONTROL_PREFIX.match('bug import malformed')
    assert _CONTROL_PREFIX.match('BUG')
    assert not _CONTROL_PREFIX.match('buggy application discussion')


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_gateway_routes_bug_to_real_control_cli_without_model(conn,config,tmp_path,monkeypatch,channel):
    import asyncio
    from types import SimpleNamespace

    import yaml
    from test_hermes_control_plugin import FakeAdapter, wait_for_receipts, write_runtime

    from k3_support.hermes_plugin import pre_gateway_dispatch

    bug=binding(conn);observe(conn,bug)
    msg=message(config,'bug '+bug['bug_id'],channel)
    config.path.write_text(yaml.safe_dump(config.raw))
    runtime=write_runtime(tmp_path,config.path)
    monkeypatch.setenv('K3_SUPPORT_CONTROL_PLUGIN_CONFIG',str(runtime))

    async def exercise():
        adapter=FakeAdapter()
        gateway=SimpleNamespace(adapters={channel:adapter})
        event=SimpleNamespace(text=msg.text,message_id=msg.message_id,raw_message=None,
            source=SimpleNamespace(platform=channel,user_id=msg.user_id,chat_id=msg.chat_id))
        assert pre_gateway_dispatch(event,gateway)['action']=='skip'
        await wait_for_receipts(adapter)
        assert '远端状态（缓存）：actual-state-id' in adapter.sent[0][1]
        assert bug['bug_id'] in adapter.sent[0][1]
    asyncio.run(exercise())


@pytest.mark.parametrize('channel',['telegram','feishu'])
def test_old_pause_after_resume_does_not_cancel_new_work(conn,config,channel):
    from k3_support.store import enqueue_outbox, transition_case

    bug=binding(conn)
    transition_case(conn,case_id=bug['case_id'],after='triage',actor_type='system',actor_id='owner',reason='setup',expected_version=1)
    pause=message(config,f"bug pause {bug['bug_id']} 2",channel,'pause-original')
    result=execute_control(conn,config,pause,control_channel=channel)
    resume=message(config,f"bug resume {bug['bug_id']} {result['version']}",channel,'resume-original')
    resumed=execute_control(conn,config,resume,control_channel=channel)
    pending,_=enqueue_outbox(conn,channel='feishu_im',action_type='reply',destination='fixture',payload={},idempotency_key='new-reply',case_id=bug['case_id'])
    replay=execute_control(conn,config,pause,control_channel=channel)
    assert replay['replayed'] and replay['state']==resumed['state']=='triage'
    assert conn.execute('SELECT state FROM outbox WHERE outbox_id=?',(pending,)).fetchone()[0]=='pending'
    assert '未再次执行' in _format_receipt(True,replay)
    assert execute_control(conn,config,resume,control_channel=channel)['replayed']


def test_chat_list_paginates_without_skipping_bound_bugs(conn,config):
    ids={binding(conn,item=str(i))['bug_id'] for i in range(1,10)}
    first=execute_control(conn,config,message(config,'bug list'))['text']
    command=next(line.removeprefix('下一页：') for line in first.splitlines() if line.startswith('下一页：'))
    second=execute_control(conn,config,message(config,command,message_id='page-2'))['text']
    displayed=[line.removeprefix('查看：bug ') for text in [first,second] for line in text.splitlines() if line.startswith('查看：bug ')]
    assert len(displayed)==9 and set(displayed)==ids
    assert len(first)<4000 and '下一页：' not in second
