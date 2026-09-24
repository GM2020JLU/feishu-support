import sys
from dataclasses import replace

import pytest

from k3_support.broker_execution_contract import ExecutionContract
from k3_support.broker_hermes import executor

RUNTIME = r'''
import json,os,pathlib,runpy,sys,types
assert sys.argv[1]=='-I'
assert pathlib.Path(sys.argv[2]).name=='hermes-coding.py'
home=pathlib.Path(os.environ['HERMES_HOME'])
assert home==pathlib.Path(os.environ['HOME'])
config=json.loads((home/'config.yaml').read_text())
assert config['tools']['tool_search']['enabled']=='off'
assert config['mcp_servers']['k3_remote']['env']['K3_SUPPORT_BROKER_TASK']=='private identity'
behavior=os.environ.get('FIXTURE_BEHAVIOR','ok')
expected={'mcp__k3_remote__'+name for name in ('verification_list','remote_read','remote_submit','board_read','board_submit')}
class Agent:
 def __init__(self,**kw):
  assert kw['api_key']=='private-fixture-key'
  assert kw['model']=='bound-model'
  assert kw['base_url']=='https://bound.example/v1'
  assert kw['enabled_toolsets']==['mcp-k3_remote']
  assert kw['skip_context_files'] and kw['skip_memory']
  assert kw['fallback_model'] is None and kw['credential_pool'] is None
  self.__dict__.update(kw);self.valid_tool_names=expected
  if behavior=='identity':self.model='wrong-model'
  if behavior=='tools':self.valid_tool_names=expected|{'terminal'}
  pathlib.Path('started').write_text('yes')
 def run_conversation(self,prompt):
  assert 'private task' in prompt
  assert b'private task' not in pathlib.Path('/proc/self/cmdline').read_bytes()
  if behavior=='tool-failed':self.tool_complete_callback('id','remote_read',{},json.dumps({'error':'rejected'}))
  if behavior=='exception':raise RuntimeError('private-fixture-key private task private identity')
  result={'completed':True,'final_response':'fixture report'}
  if behavior in ('partial','failed','interrupted'):result[behavior]=True
  if behavior=='incomplete':result['completed']=False
  if behavior=='empty':result['final_response']=''
  return result
 def close(self):pathlib.Path('closed').write_text('yes')
run_agent=types.ModuleType('run_agent');run_agent.AIAgent=Agent
sys.modules['run_agent']=run_agent
mcp=types.ModuleType('tools.mcp_tool')
mcp.discover_mcp_tools=lambda:expected if behavior!='missing-tools' else set()
mcp.shutdown_mcp_servers=lambda:pathlib.Path('shutdown').write_text('yes')
sys.modules['tools']=types.ModuleType('tools');sys.modules['tools.mcp_tool']=mcp
runpy.run_path(sys.argv[2],run_name='__main__')
'''


def setup_adapter(tmp_path, behavior='ok', contract=None):
    binary = tmp_path / 'python-hermes-fixture'
    binary.write_text(f'#!{sys.executable}\n' + RUNTIME)
    binary.chmod(0o700)
    home = tmp_path / 'credentials'
    home.mkdir()
    (home / 'api-key').write_text('private-fixture-key')
    policy = contract or ExecutionContract('fixture', 'https://bound.example/v1', 'bound-model',
                                          'high', 'chat_completions', 'a'*64, 'hermes')
    execute = executor(executable=str(binary), workdir=str(tmp_path), remote_python=sys.executable,
                       contract=policy, environment={'HOME': str(home), 'K3_SUPPORT_AGENT_HOME': str(home),
                        'K3_SUPPORT_BROKER_TASK': 'private identity', 'K3_SUPPORT_BROKER_SOCKET': str(tmp_path/'absent'),
                        'K3_SUPPORT_BROKER_CONTROL_UID': '1', 'FIXTURE_BEHAVIOR': behavior})
    return execute, {'execution': policy.selection(), 'model': policy.model,
                     'reasoning': policy.reasoning, 'brief': 'private task'}


def test_native_bridge_contract_report_and_cleanup(tmp_path):
    execute, inputs = setup_adapter(tmp_path)
    assert execute(inputs, lambda: None) == 'fixture report'
    assert (tmp_path/'closed').exists() and (tmp_path/'shutdown').exists()
    assert not list(tmp_path.glob('.hermes-*'))
    assert (tmp_path/'credentials/api-key').read_text() == 'private-fixture-key'


@pytest.mark.parametrize('behavior', ['identity','tools','partial','failed','interrupted',
                                       'incomplete','empty','tool-failed','missing-tools'])
def test_unverified_execution_is_never_success(tmp_path, behavior):
    execute, inputs = setup_adapter(tmp_path, behavior)
    with pytest.raises(ValueError, match='worker command failed'):
        execute(inputs, lambda: None)
    assert (tmp_path/'shutdown').exists()
    assert not list(tmp_path.glob('.hermes-*'))


@pytest.mark.parametrize('field,value', [('execution',{}),('model','other'),('reasoning','low')])
def test_mismatched_task_cannot_start(tmp_path, field, value):
    execute, inputs = setup_adapter(tmp_path)
    inputs[field] = value
    with pytest.raises(ValueError, match='does not match'):
        execute(inputs, lambda: None)
    assert not (tmp_path/'started').exists()


@pytest.mark.parametrize('override', [{'agent':'codex'}, {'wire_api':'responses'}, {'reasoning':'unknown'}])
def test_unsupported_contract_rejected(tmp_path, override):
    policy = ExecutionContract('fixture', 'https://bound.example/v1', 'bound-model',
                               'high', 'chat_completions', 'a'*64, 'hermes')
    with pytest.raises(ValueError, match='validated Hermes'):
        setup_adapter(tmp_path, contract=replace(policy, **override))


@pytest.mark.parametrize('behavior,code', [
    ('exception', 'conversation_exception'), ('partial', 'completion_partial'),
    ('failed', 'completion_failed'), ('interrupted', 'completion_interrupted'),
    ('incomplete', 'completion_incomplete'), ('empty', 'report_missing'),
    ('tool-failed', 'broker_tool_failed'), ('identity', 'execution_identity_changed'),
    ('missing-tools', 'broker_tools_unavailable'),
])
def test_failure_diagnostics_are_fixed_codes_without_private_data(tmp_path, monkeypatch, behavior, code):
    from k3_support import broker_hermes
    from k3_support.broker_process import run_process

    captured = []
    def capture(**kwargs):
        result = run_process(**{**kwargs, "detailed": True})
        captured.append(result)
        raise ValueError('expected fixture failure')
    monkeypatch.setattr(broker_hermes, 'run_process', capture)
    execute, inputs = setup_adapter(tmp_path, behavior)
    with pytest.raises(ValueError, match='expected fixture failure'):
        execute(inputs, lambda: None)
    assert captured[0]['exit_code'] == 2
    assert captured[0]['stdout'] == ''
    assert captured[0]['stderr'] == 'Hermes coding bridge failed: ' + code + '\n'
    assert not list(tmp_path.glob('.hermes-*'))


@pytest.mark.parametrize('stderr,message', [
    ('Hermes coding bridge failed: completion_partial\n', 'worker command failed: hermes completion_partial'),
    ('PRIVATE provider error with private-fixture-key', 'worker command failed'),
    ('Hermes coding bridge failed: private-fixture-key', 'worker command failed'),
    ('Hermes coding bridge failed: completion_partial\nPRIVATE', 'worker command failed'),
])
def test_adapter_only_surfaces_allowlisted_failure_codes(tmp_path, monkeypatch, stderr, message):
    monkeypatch.setattr('k3_support.broker_hermes.run_process',
                        lambda **kw: {'exit_code': 2, 'stderr': stderr, 'stdout': 'PRIVATE'})
    execute, inputs = setup_adapter(tmp_path)
    with pytest.raises(ValueError) as error:
        execute(inputs, lambda: None)
    assert str(error.value) == message
