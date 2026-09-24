import sys
from dataclasses import replace

import pytest

from k3_support.broker_dsh import executor
from k3_support.broker_execution_contract import ExecutionContract
from k3_support.coding_acp import ACPError

SERVER = r'''
import json, os, pathlib, sys
assert sys.argv[1:] == ['--profile', 'k3-broker']
home=pathlib.Path(os.environ['DSH_HOME'])
profile=home/'profiles/k3-broker'
manifest=json.loads((profile/'package.json').read_text())
assert manifest['dsh']['profile'] == {'bundles': [], 'patchReload': 'startup'}
rows=json.loads((profile/'cordis.patch.yml').read_text())[0]['insert']
assert not any('dsh-tool-' in row['name'] for row in rows)
assert not any(row['id'] in ('settings', 'llm-pi-ai') for row in rows)
llm=next(row['config'] for row in rows if row['id']=='llm-deepseek')
assert llm['baseURL']==os.environ['DEEPSEEK_BASE_URL']=='https://bound.example/v1'
assert llm['models']==[{'id':'bound-model'}]
assert llm['reasoningEffort']=='high'
assert 'private task' not in pathlib.Path('/proc/self/cmdline').read_bytes().decode()
pathlib.Path('started').write_text(str(home))
def send(value): print(json.dumps({'jsonrpc':'2.0',**value}),flush=True)
options=[{'id':'model','type':'select','currentValue':'["deepseek-official","bound-model"]',
          'options':[{'value':'["deepseek-official","bound-model"]'}]},
         {'id':'reasoning_effort','type':'select','currentValue':'high','options':[{'value':'high'}]}]
for line in sys.stdin:
 request=json.loads(line);method=request.get('method');params=request.get('params',{})
 result={}
 if method=='initialize': result={'protocolVersion':1}
 elif method=='session/new':
  mcp=params['mcpServers'];assert len(mcp)==1
  assert mcp[0]['name']=='k3_remote'
  assert mcp[0]['args']==['-I','-m','k3_support.broker_remote_mcp']
  assert {p['name']:p['value'] for p in mcp[0]['env']}['K3_SUPPORT_BROKER_TASK']=='private task identity'
  result={'sessionId':'fixture','configOptions':options}
 elif method=='session/set_config_option':
  assert any(p['id']==params['configId'] and p['currentValue']==params['value'] for p in options)
  result={'configOptions':options}
 elif method=='session/prompt':
  assert 'private task' in params['prompt'][0]['text']
  behavior=os.environ.get('FIXTURE_BEHAVIOR','ok')
  if behavior=='deny':
   send({'id':'deny','method':'session/request_permission','params':{'sessionId':'fixture','options':[]}})
   answer=json.loads(sys.stdin.readline());assert answer['result']['outcome']['outcome']=='cancelled'
  if behavior=='boundaries':
   updates=[{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'Progress without trailing newline.'}},
            {'sessionUpdate':'tool_call','toolCallId':'read','status':'in_progress'},
            {'sessionUpdate':'tool_call_update','toolCallId':'read','status':'completed'},
            {'sessionUpdate':'agent_thought_chunk','content':{'type':'text','text':'PRIVATE THOUGHT'}}]
   for text in ('sta', 'tus\ncompleted\n', *[name+'\nnone\n' for name in ('root_cause','changes','verification','board_state','push_state','artifacts','risks','next_action','reply_draft')]):
    updates.append({'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':text}})
   for update in updates:send({'method':'session/update','params':{'sessionId':'fixture','update':update}})
  elif behavior!='empty':
   send({'method':'session/update','params':{'sessionId':'fixture','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'verified fixture report'}}}})
  result={'stopReason':'end_turn'}
 elif method=='session/close': pathlib.Path('closed').write_text('yes')
 send({'id':request['id'],'result':result})
'''


def setup_adapter(tmp_path, behavior='ok', contract=None):
    binary = tmp_path / 'python-dsh-fixture'
    binary.write_text(f'#!{sys.executable}\n' + SERVER)
    binary.chmod(0o700)
    credentials = tmp_path / 'credentials'
    credentials.mkdir()
    marker = credentials / '.credentials.yaml'
    marker.write_text('preserve native credentials')
    policy = contract or ExecutionContract('fixture', 'https://bound.example/v1', 'bound-model',
                                          'high', 'chat_completions', 'a'*64, 'dsh')
    env = {'HOME': str(credentials), 'DSH_HOME': str(credentials),
           'K3_SUPPORT_BROKER_TASK': 'private task identity',
           'K3_SUPPORT_BROKER_SOCKET': str(tmp_path / 'absent.sock'),
           'K3_SUPPORT_BROKER_CONTROL_UID': '1', 'FIXTURE_BEHAVIOR': behavior}
    execute = executor(executable=str(binary), workdir=str(tmp_path), environment=env,
                       contract=policy, remote_python=sys.executable)
    inputs = {'execution': policy.selection(), 'model': policy.model,
              'reasoning': policy.reasoning, 'brief': 'private task'}
    return execute, inputs


def test_full_adapter_protocol_binds_profile_and_cleans_private_session(tmp_path):
    execute, inputs = setup_adapter(tmp_path)
    assert execute(inputs, lambda: None) == 'verified fixture report'
    assert (tmp_path / 'closed').exists()
    assert not list(tmp_path.glob('.dsh-*'))
    assert (tmp_path / 'credentials/.credentials.yaml').read_text() == 'preserve native credentials'


@pytest.mark.parametrize('behavior', ['empty', 'deny'])
def test_uncertain_result_never_becomes_success(tmp_path, behavior):
    execute, inputs = setup_adapter(tmp_path, behavior)
    with pytest.raises(ACPError):
        execute(inputs, lambda: None)
    assert not list(tmp_path.glob('.dsh-*'))


@pytest.mark.parametrize('field,value', [('execution', {}), ('model', 'other'), ('reasoning', 'max')])
def test_mismatched_input_does_not_start_native_process(tmp_path, field, value):
    execute, inputs = setup_adapter(tmp_path)
    inputs[field] = value
    with pytest.raises(ValueError, match='does not match'):
        execute(inputs, lambda: None)
    assert not (tmp_path / 'started').exists()


@pytest.mark.parametrize('overrides', [{'agent':'hermes'}, {'wire_api':'messages'}, {'reasoning':'medium'}])
def test_unsupported_native_contract_is_rejected(tmp_path, overrides):
    policy = ExecutionContract('fixture', 'https://bound.example/v1', 'bound-model',
                               'high', 'chat_completions', 'a'*64, 'dsh')
    with pytest.raises(ValueError, match='validated DSH'):
        setup_adapter(tmp_path, contract=replace(policy, **overrides))


def test_progress_and_fragmented_final_heading_remain_separate(tmp_path):
    from k3_support.executors import validate_codex_result_text

    execute, inputs = setup_adapter(tmp_path, behavior="boundaries")
    text = execute(inputs, lambda: None)
    assert "Progress without trailing newline.\n\nstatus\ncompleted" in text
    assert "PRIVATE THOUGHT" not in text
    assert validate_codex_result_text(text)["status"] == "completed"
