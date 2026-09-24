import sys
from dataclasses import replace

import pytest

from k3_support.broker_execution_contract import ExecutionContract
from k3_support.broker_opencode import executor
from k3_support.coding_acp import ACPError

SERVER = r'''
import json, os, pathlib, sys
if sys.argv[1:] == ['--version']:
 print('unsupported' if os.environ.get('FAKE_VERSION')=='bad' else 'opencode v2.0.12' if os.environ.get('FAKE_VERSION')=='2' else '1.18.29')
 raise SystemExit(0)
assert sys.argv[1:] == ['acp']
home=pathlib.Path(os.environ['HOME'])
config=json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])
if os.environ.get('FAKE_VERSION')=='2':
 assert pathlib.Path.cwd()==home and (home/'.git').is_dir()
 assert config['permissions']==[{'action':'*','resource':'*','effect':'deny'}, {'action':'k3_remote_*','resource':'*','effect':'allow'}]
 assert config['mcp']['servers']['k3_remote']=={'type':'local','codemode':False,
   'command':[sys.executable,'-I','-m','k3_support.broker_remote_mcp'],
   'environment':{'K3_SUPPORT_BROKER_TASK':'private task identity',
                  'K3_SUPPORT_BROKER_SOCKET':os.environ['K3_SUPPORT_BROKER_SOCKET'],
                  'K3_SUPPORT_BROKER_CONTROL_UID':'1'}}
 assert config['providers']['k3-broker']['settings']['baseURL']=='https://bound.example/v1'
 assert config['providers']['k3-broker']['models']['bound-model']['variants']==[{'id':'high','settings':{'reasoningEffort':'high'}}]
 assert config['providers']['k3-broker']['settings']['apiKey']=='{env:K3_BROKER_API_KEY}'
 assert os.environ['K3_BROKER_API_KEY']=='preserve API key'
else:
 assert config['permission']=={'*':'deny','k3_remote_*':'allow'}
 assert config['enabled_providers']==['k3-broker']
 assert config['provider']['k3-broker']['options']['baseURL']=='https://bound.example/v1'
 assert config['provider']['k3-broker']['models']['bound-model']['variants']=={'high':{'reasoningEffort':'high'}}
assert os.environ['OPENCODE_DISABLE_PROJECT_CONFIG']=='true'
assert os.environ['OPENCODE_DISABLE_DEFAULT_PLUGINS']=='true'
assert os.environ['XDG_CONFIG_HOME'].startswith(str(home))
assert 'private task' not in pathlib.Path('/proc/self/cmdline').read_bytes().decode()
pathlib.Path('started').write_text(str(home))
def send(value): print(json.dumps({'jsonrpc':'2.0',**value}),flush=True)
options=[{'id':'model','type':'select','currentValue':'k3-broker/bound-model',
          'options':[{'value':'k3-broker/bound-model'}]},
         {'id':'effort','type':'select','currentValue':'high','options':[{'value':'high'}]}]
for line in sys.stdin:
 request=json.loads(line);method=request.get('method');params=request.get('params',{})
 result={}
 if method=='initialize': result={'protocolVersion':1}
 elif method=='session/new':
  mcp=params['mcpServers']
  if os.environ.get('FAKE_VERSION')=='2': assert mcp==[]
  else:
   assert len(mcp)==1
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


def setup_adapter(tmp_path, behavior='ok', contract=None, version=1):
    binary = tmp_path / 'python-opencode-fixture'
    binary.write_text(f'#!{sys.executable}\n' + SERVER)
    binary.chmod(0o700)
    credentials = tmp_path / 'credentials'
    credentials.mkdir()
    marker = credentials / 'api-key'
    marker.write_text('preserve API key')
    policy = contract or ExecutionContract('fixture', 'https://bound.example/v1', 'bound-model',
                                          'high', 'chat_completions', 'a'*64, 'opencode')
    env = {'HOME': str(credentials), 'K3_SUPPORT_AGENT_HOME': str(credentials),
           'K3_SUPPORT_BROKER_TASK': 'private task identity',
           'K3_SUPPORT_BROKER_SOCKET': str(tmp_path / 'absent.sock'),
           'K3_SUPPORT_BROKER_CONTROL_UID': '1', 'FIXTURE_BEHAVIOR': behavior,
           'FAKE_VERSION': str(version)}
    execute = executor(executable=str(binary), workdir=str(tmp_path), environment=env,
                       contract=policy, remote_python=sys.executable)
    inputs = {'execution': policy.selection(), 'model': policy.model,
              'reasoning': policy.reasoning, 'brief': 'private task'}
    return execute, inputs


def test_full_adapter_protocol_binds_profile_and_cleans_private_session(tmp_path):
    execute, inputs = setup_adapter(tmp_path)
    assert execute(inputs, lambda: None) == 'verified fixture report'
    assert (tmp_path / 'closed').exists()
    assert not list(tmp_path.glob('.opencode-*'))
    assert (tmp_path / 'credentials/api-key').read_text() == 'preserve API key'


def test_v2_uses_private_git_location_and_scoped_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr('k3_support.broker_opencode.time.sleep', lambda _: None)
    execute, inputs = setup_adapter(tmp_path, version=2)
    assert execute(inputs, lambda: None) == 'verified fixture report'
    assert not list(tmp_path.glob('.opencode-*'))


def test_unknown_opencode_version_fails_before_acp(tmp_path):
    execute, inputs = setup_adapter(tmp_path, version='bad')
    with pytest.raises(ValueError, match='unsupported OpenCode version'):
        execute(inputs, lambda: None)
    assert not (tmp_path / 'started').exists()


@pytest.mark.parametrize('behavior', ['empty', 'deny'])
def test_uncertain_result_never_becomes_success(tmp_path, behavior):
    execute, inputs = setup_adapter(tmp_path, behavior)
    with pytest.raises(ACPError):
        execute(inputs, lambda: None)
    assert not list(tmp_path.glob('.opencode-*'))


@pytest.mark.parametrize('field,value', [('execution', {}), ('model', 'other'), ('reasoning', 'max')])
def test_mismatched_input_does_not_start_native_process(tmp_path, field, value):
    execute, inputs = setup_adapter(tmp_path)
    inputs[field] = value
    with pytest.raises(ValueError, match='does not match'):
        execute(inputs, lambda: None)
    assert not (tmp_path / 'started').exists()


@pytest.mark.parametrize('overrides', [{'agent':'hermes'}, {'wire_api':'messages'}, {'reasoning':'unsupported'}])
def test_unsupported_native_contract_is_rejected(tmp_path, overrides):
    policy = ExecutionContract('fixture', 'https://bound.example/v1', 'bound-model',
                               'high', 'chat_completions', 'a'*64, 'opencode')
    with pytest.raises(ValueError, match='validated OpenCode'):
        setup_adapter(tmp_path, contract=replace(policy, **overrides))


def test_progress_and_fragmented_final_heading_remain_separate(tmp_path):
    from k3_support.executors import validate_codex_result_text

    execute, inputs = setup_adapter(tmp_path, behavior="boundaries")
    text = execute(inputs, lambda: None)
    assert "Progress without trailing newline.\n\nstatus\ncompleted" in text
    assert "PRIVATE THOUGHT" not in text
    assert validate_codex_result_text(text)["status"] == "completed"
