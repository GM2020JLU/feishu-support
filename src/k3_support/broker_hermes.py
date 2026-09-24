"""Hermes native AIAgent bridge; semantic-inference bridge remains separate."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .broker_execution_contract import ExecutionContract
from .broker_process import run_process


def executor(*, executable, workdir, environment, contract, remote_python):
    if (type(contract) is not ExecutionContract or contract.agent != 'hermes'
            or contract.wire_api != 'chat_completions'
            or contract.reasoning not in {'low', 'medium', 'high', 'xhigh', 'max'}):
        raise ValueError('validated Hermes chat-completions contract required')
    env = dict(environment)
    credential_home = env.get('K3_SUPPORT_AGENT_HOME', '')
    if any(not Path(v).is_absolute() for v in (executable, workdir, remote_python, credential_home)):
        raise ValueError('absolute deployment paths required')
    private_names = ('K3_SUPPORT_BROKER_TASK', 'K3_SUPPORT_BROKER_SOCKET', 'K3_SUPPORT_BROKER_CONTROL_UID')
    if any(not isinstance(env.get(key), str) or not env[key] for key in private_names):
        raise ValueError('private broker task environment required')
    bridge = Path(__file__).parent / 'agent_bridges' / 'hermes-coding.py'
    config = {'model': {'default': contract.model, 'provider': 'custom', 'base_url': contract.base_url},
              'mcp_servers': {'k3_remote': {'command': str(remote_python),
                'args': ['-I', '-m', 'k3_support.broker_remote_mcp'],
                'env': {key: env[key] for key in private_names}}},
              'tools': {'tool_search': {'enabled': 'off'}},
              'fallback_models': [], 'memory': {'memory_enabled': False, 'user_profile_enabled': False}}

    def execute(inputs, heartbeat):
        if (inputs.get('execution') != contract.selection() or inputs.get('model') != contract.model
                or inputs.get('reasoning') != contract.reasoning):
            raise ValueError('task does not match configured Hermes adapter')
        brief = inputs.get('brief')
        if not isinstance(brief, str) or not brief.strip():
            raise ValueError('task brief required')
        from .broker_verification_instructions import task_guidance
        brief += task_guidance(inputs)
        request = {'model': contract.model, 'base_url': contract.base_url, 'reasoning': contract.reasoning,
                   'credential_file': str(Path(credential_home) / 'api-key'),
                   'brief': brief + '\n\nUse only k3_remote MCP tools for repository operations. '
                    'Record a canonical UUID request_id before remote_submit; use mode inspect or work '
                    'and an assigned repo for work. Poll remote_read with a fresh query request_id and '
                    'the original remote_request_id, no more often than every 2 seconds; follow next_offset. '
                    'Never repeat an operation on unknown delivery. Report original IDs and uncertainty. '
                    'Board operations still require broker-verified session approval.\n'}
        with TemporaryDirectory(prefix='.hermes-', dir=workdir) as home:
            (Path(home) / 'config.yaml').write_text(json.dumps(config))
            child_env = {**env, 'HOME': home, 'HERMES_HOME': home,
                         **{key: str(Path(home) / key.lower()) for key in
                            ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME')}}
            completed = run_process(argv=[str(executable), '-I', str(bridge)], cwd=str(workdir), env=child_env,
                              stdin=json.dumps(request).encode(), heartbeat=heartbeat,
                              timeout=7200, heartbeat_interval=10, detailed=True)
        if completed['exit_code'] != 0:
            codes = {'credential_unavailable', 'broker_tools_unavailable', 'execution_identity_changed',
                     'conversation_exception', 'invalid_completion_envelope', 'completion_failed',
                     'completion_partial', 'completion_interrupted', 'completion_incomplete',
                     'broker_tool_failed', 'report_missing'}
            diagnostic = completed['stderr'].strip()
            prefix = 'Hermes coding bridge failed: '
            code = diagnostic.removeprefix(prefix)
            if diagnostic.startswith(prefix) and code in codes:
                raise ValueError('worker command failed: hermes ' + code)
            raise ValueError('worker command failed')
        raw = completed['stdout']
        try:
            result = json.loads(raw)
        except (ValueError, RecursionError):
            raise ValueError('invalid Hermes completion envelope') from None
        if not isinstance(result, dict) or set(result) != {'result'} or not isinstance(result['result'], str) or not result['result'].strip():
            raise ValueError('Hermes did not return a report')
        return result['result']

    return execute
