"""Authenticated chat admission to the existing Bug investigation job path."""

import json

from . import coding_tasks, project_investigation
from .ids import digest
from .project_bugs import detail

COMMANDS = {'coding-options', 'investigate', 'start-round'}
USAGE = ('bug investigate <Bug ID> <调查轮次 ID> <仓库> <工具 ID> <分支> '
         '<完整提交 SHA> "任务说明" "验收要求" [同轮次已结束的前序任务 ID]')


def _replay(conn, config, request_id, origin):
    row = conn.execute(
        "SELECT j.job_id,j.state,j.context_json,j.input_digest,b.payload_json FROM jobs j "
        "JOIN broker_inputs b USING(job_id) WHERE j.job_type='codex' "
        "AND json_valid(j.context_json) AND json_extract(j.context_json,'$.operator_request.actor')=? "
        "AND json_extract(j.context_json,'$.operator_request.request_id')=?",
        (config.control_operator_id, request_id),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row['payload_json'])
    context = json.loads(row['context_json'])
    recorded = context['operator_request']
    if (digest(payload) != row['input_digest'] or recorded.get('origin') != origin
            or payload['context_extra']['operator_request'] != recorded):
        raise ValueError('同一聊天消息已用于不同任务，不能修改后重放')
    return {'job_id': row['job_id'], 'state': row['state'], 'created': False}


def route(conn, config, argv, request_id, channel):
    from .control import ControlError

    if argv[1] == 'start-round':
        if len(argv) != 5 or not argv[3].isdigit():
            raise ControlError('bug start-round <Bug ID> <Bug 版本> "调查原因"')
        from .project_bug_controls import execute

        value = execute(conn, config, action='start-round', payload={
            'bug_id': argv[2], 'expected_revision': int(argv[3]),
            'reason': argv[4], 'request_id': request_id,
        })
        return {'command': 'project_bug', 'text':
                f"本地调查轮次：{value['round_id']}\n查看任务选项：bug coding-options {argv[2]}\n"
                '已记录调查轮次，不会改变远端 Bug 状态或自动执行代码。'}
    if argv[1] == 'coding-options' and len(argv) == 3:
        bug = detail(conn, argv[2])
        options = coding_tasks.options(conn, config, {'case_id': bug['case_id']})
        lines = ['Bug 调查任务可选项', f"Bug 版本：{bug['revision']}",
                 f"新调查轮次：bug start-round {bug['bug_id']} {bug['revision']} \"调查原因\"", '仓库：' + '、'.join(options['repositories'])]
        for item in options['items']:
            lines.append(f"工具：{item['id']} · {item['label']} · {item['status']}")
        active = next((r for r in bug['rounds'] if r['archived_at'] is None), None)
        lines.append('当前调查轮次：' + (active['round_id'] if active else '尚未建立'))
        lines.extend([USAGE, '接续另一仓库时在最后附上同轮次已结束的前序任务 ID。'
                      '必须明确完整提交和验收要求；本命令不写入飞书项目。'])
        return {'command': 'project_bug', 'text': '\n'.join(lines)}
    if argv[1] != 'investigate' or len(argv) not in {10, 11}:
        raise ControlError(USAGE)
    _, _, bug_id, round_id, repository, executor_id, branch, commit, instructions, acceptance = argv[:10]
    predecessor_job_id = argv[10] if len(argv) == 11 else None
    origin = {'channel': channel, 'intent_digest': digest(argv)}
    result = _replay(conn, config, request_id, origin)
    if result is None:
        bug = detail(conn, bug_id)
        active = [r for r in bug['rounds'] if r['archived_at'] is None]
        if len(active) != 1 or active[0]['round_id'] != round_id:
            raise ControlError('请先通过 bug coding-options 查看并建立当前调查轮次；不会自动重开 Bug。')
        options = coding_tasks.options(conn, config, {'case_id': bug['case_id']})
        tool = next((x for x in options['items'] if x['id'] == executor_id and x['status'] == 'configured'), None)
        if tool is None or repository not in options['source_choices']:
            raise ControlError('仓库或编码工具不可用，请先查看 bug coding-options ' + bug_id)
        payload = {
            'bug_id': bug_id, 'round_id': active[0]['round_id'], 'expected_revision': bug['revision'],
            'case_version': options['case_version'], 'repository': repository,
            'executor_id': executor_id, 'contract_fingerprint': tool['contract_fingerprint'],
            'instructions': instructions, 'acceptance': acceptance, 'request_id': request_id,
            'source': {**options['source_choices'][repository], 'branch': branch,
                       'base_commit': commit, 'version': ''},
        }
        if predecessor_job_id is not None:
            payload['predecessor_job_id'] = predecessor_job_id
        try:
            result = project_investigation.submit(conn, config, payload, request_origin=origin)
        except (ValueError, RuntimeError):
            # A competing delivery may have committed while this one was resolving
            # fresh versions. Only the same immutable native message can replay.
            result = _replay(conn, config, request_id, origin)
            if result is None:
                raise
    return {'command': 'project_bug', 'text':
            f"调查任务：{result['job_id']}\n状态：{result['state']}\n"
            f"{'已创建' if result['created'] else '已存在，未重复创建'}\n查看进度：bug progress {bug_id}\n"
            '使用配置节点和独立源码副本；执行成功不代表修复或验证通过。不会自动推送代码或修改远端 Bug。'}
