"""Standalone Python 3.11+ bridge executed by the trusted Hermes interpreter."""
import contextlib
import json
import logging
import os
import sys
from pathlib import Path


class BridgeFailure(ValueError):
    """Only fixed, non-sensitive diagnostic codes may cross the bridge."""


def execute(request):
    from run_agent import AIAgent
    from tools.mcp_tool import discover_mcp_tools, shutdown_mcp_servers

    expected = {'mcp__k3_remote__' + name for name in
                ('verification_list', 'remote_submit', 'remote_read', 'board_submit', 'board_read')}
    failed_tools = []

    def tool_complete(_id, _name, _args, result):
        try:
            value = json.loads(result) if isinstance(result, str) else result
        except (ValueError, TypeError):
            return
        if isinstance(value, dict) and (value.get('error') or value.get('isError') or value.get('success') is False):
            failed_tools.append(True)

    with Path(request['credential_file']).open() as stream:
        raw_key = stream.read(16385)
    key = raw_key.strip()
    if not key or len(raw_key) > 16384 or '\0' in key:
        raise BridgeFailure('credential_unavailable')
    agent = None
    try:
        discovered = set(discover_mcp_tools())
        if discovered != expected:
            raise BridgeFailure('broker_tools_unavailable')
        agent = AIAgent(model=request['model'], provider='custom', requested_provider='custom',
                        api_mode='chat_completions', base_url=request['base_url'], api_key=key,
                        reasoning_config={'enabled': True, 'effort': request['reasoning']},
                        enabled_toolsets=['mcp-k3_remote'], quiet_mode=True,
                        tool_complete_callback=tool_complete, skip_context_files=True,
                        skip_memory=True, load_soul_identity=False, save_trajectories=False,
                        fallback_model=None, credential_pool=None, checkpoints_enabled=False)
        if (agent.valid_tool_names != expected or agent.model != request['model']
                or agent.base_url != request['base_url'] or agent.provider != 'custom'
                or agent.api_mode != 'chat_completions'
                or agent.reasoning_config != {'enabled': True, 'effort': request['reasoning']}):
            raise BridgeFailure('execution_identity_changed')
        try:
            result = agent.run_conversation(request['brief'])
        except (Exception, SystemExit):  # noqa: BLE001 -- native errors may contain credentials or task data
            raise BridgeFailure('conversation_exception') from None
        if not isinstance(result, dict):
            raise BridgeFailure('invalid_completion_envelope')
        for flag in ('failed', 'partial', 'interrupted'):
            if result.get(flag):
                raise BridgeFailure('completion_' + flag)
        if result.get('completed') is not True:
            raise BridgeFailure('completion_incomplete')
        if failed_tools:
            raise BridgeFailure('broker_tool_failed')
        if not isinstance(result.get('final_response'), str) or not result['final_response'].strip():
            raise BridgeFailure('report_missing')
        return {'result': result['final_response']}
    finally:
        try:
            if agent is not None:
                agent.close()
        finally:
            shutdown_mcp_servers()


def main():
    try:
        raw = sys.stdin.buffer.read(2097153)
        if len(raw) > 2097152:
            raise ValueError('input too large')
        request = json.loads(raw)
        if (not isinstance(request, dict)
                or set(request) != {'model', 'base_url', 'reasoning', 'brief', 'credential_file'}
                or any(not isinstance(value, str) or not value or '\0' in value for value in request.values())
                or not Path(request['credential_file']).is_absolute()):
            raise ValueError('invalid request')
        logging.disable(logging.CRITICAL)
        with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            result = execute(request)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except BridgeFailure as exc:
        print('Hermes coding bridge failed: ' + str(exc), file=sys.stderr)
        return 2
    except (Exception, SystemExit):  # noqa: BLE001 - redact native errors containing tasks or credentials
        print('Hermes coding bridge failed', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
