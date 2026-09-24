"""Authenticated chat navigation over the same Bug and Case controls as the GUI."""

import json
import shlex

from .ids import digest
from .project_bug_controls import execute

_CHAT_JOB_TOOLS = {
    "codex": "Codex",
    "claude": "Claude Code",
    "dsh": "DeepSeek Harness",
    "opencode": "OpenCode",
    "hermes": "Hermes",
}
_CHAT_JOB_STATES = {
    "queued": "等待执行",
    "running": "执行中",
    "waiting": "等待依赖",
    "succeeded": "任务执行成功（不代表修复完成）",
    "failed": "执行失败",
    "cancelled": "已取消",
    "orphaned": "执行结果待核对",
    "unknown": "状态待核对",
}


def _progress(conn, config, bug_id):
    """Bounded, read-only status from the same local projection as the GUI."""
    from .store import get_case

    detail = execute(conn, config, action='detail', payload={'bug_id': bug_id})
    case = get_case(conn, detail['case_id'])
    snapshot = detail['snapshot'] or {}
    active = next((r for r in reversed(detail['rounds']) if r['archived_at'] is None), None)
    lines = [f"Bug：{detail['bug_id']}",
             f"远端状态（缓存）：{snapshot.get('status_id', '尚未读取')}",
             f"最近观测：{snapshot.get('observed_at', '无')}",
             f"本地任务：{case['state']} · 版本 {case['version']}"]
    if active is None:
        lines.append('当前调查轮次：无；历史轮次不能直接作为新修复或验证结论。')
    else:
        lifecycle = active.get('lifecycle', {}).get('state', 'current')
        lines.extend([f"当前调查轮次：{active['round_id']} · {lifecycle}",
                      f"执行：{active.get('execution_state', '尚未开始')}",
                      f"修复：{active.get('repair_state', '尚未确认')}",
                      f"验证：{active.get('verification_state', '尚未验证')}"])
        settlement = active.get('settlement') or {}
        if settlement.get('blocker_count'):
            reasons = [f"{item['kind']}/{item['state']}" for item in settlement.get('blockers', [])[:3]]
            lines.append(f"资源待核对：{settlement['blocker_count']} 项；" + '、'.join(reasons))
    jobs = [job for job in detail.get('investigation_jobs') or []
            if active is not None and job['round_id'] == active['round_id']]
    lines.append(f"本轮编码任务：{len(jobs)} 个（最近显示 {min(5, len(jobs))} 个）")
    for job in jobs[:5]:
        state = _CHAT_JOB_STATES.get(job.get('state'), '状态待核对')
        predecessor = f" · 接续 {job['predecessor_job_id']}" if job.get('predecessor_job_id') else ''
        lines.append(f"{job['job_id']} · {_CHAT_JOB_TOOLS.get(job.get('agent'), '其他工具')} "
                     f"· {state} · 更新 {job.get('updated_at') or '未知'}{predecessor}")
        if job.get('error_class') and job.get('state') in {'failed', 'orphaned', 'unknown'}:
            lines.append('失败类别：' + str(job['error_class'])[:80])
    unsettled = [operation for operation in detail['operations']
                 if operation['state'] in {'prepared', 'dispatched', 'unknown'}]
    if unsettled:
        lines.append('待核对写入：' + '、'.join(
            f"{item['action']}/{item['state']}" for item in unsettled[:5]))
    lines.extend([f"完整详情和下一步：bug {detail['bug_id']}",
                  '以上是本地缓存与执行记录；未触发远端刷新。执行成功、修复完成、验证通过和缺陷关闭各自独立。'])
    return {'command': 'project_bug', 'text': '\n'.join(lines)}


def route(conn, config, message, argv, *, channel):
    """Caller verifies native channel identity before entering this function."""
    from .control import ControlError
    from .store import get_case

    request_id = 'chat-' + digest({'channel': channel, 'chat': message.chat_id,
                                  'message': message.message_id})
    from . import (
        project_chat_approvals,
        project_chat_create,
        project_chat_grants,
        project_chat_investigation,
        project_chat_writes,
    )

    project_chat_writes.check_intent(conn, config, argv, request_id)
    project_chat_create.check_intent(conn, config, argv, request_id)
    # Native edited/redelivered messages cannot change intent by selecting another
    # Bug command family. This only records a local attempt, not a remote effect.
    name = argv[1] if len(argv) >= 2 else ""
    lengths = {"start-round": {5}, "investigate": {10, 11}, "grant": {5},
               "claim": {3}, "delegate": {3}, "suggest-only": {3}}
    bug_id = None
    if (name in lengths and len(argv) in lengths[name]
            or name in {"pause", "resume", "takeover"} and len(argv) >= 4):
        from .project_bugs import _bug

        bug_id = _bug(conn, argv[2])["bug_id"]
    elif name in {"approve-close", "deny-close"} and len(argv) == 4:
        row = conn.execute("SELECT bug_id FROM project_close_approvals WHERE approval_id=?", (argv[2],)).fetchone()
        if row is None:
            raise ControlError("close approval does not exist")
        bug_id = row["bug_id"]
    elif name == "revoke-grant" and len(argv) == 3:
        row = conn.execute(
            "SELECT scope_json FROM project_bug_grants WHERE grant_id=? AND actor=?",
            (argv[2], config.control_operator_id),
        ).fetchone()
        if row is None:
            raise ControlError("grant is not owned by this operator")
        bug_id = json.loads(row["scope_json"])["bug_ids"][0]
    if bug_id is not None:
        project_chat_writes.reserve_intent(conn, config, bug_id=bug_id,
                                          argv=argv, request_id=request_id)

    if len(argv) >= 2 and argv[1] in project_chat_create.COMMANDS:
        return project_chat_create.route(conn, config, argv, request_id)

    if len(argv) >= 2 and argv[1] in project_chat_grants.COMMANDS:
        return project_chat_grants.route(conn, config, argv, request_id)

    if len(argv) >= 2 and argv[1] in project_chat_writes.COMMANDS:
        return project_chat_writes.route(conn, config, argv, request_id)

    if len(argv) >= 2 and argv[1] in project_chat_investigation.COMMANDS:
        return project_chat_investigation.route(conn, config, argv, request_id, channel)

    if len(argv) >= 2 and argv[1] in project_chat_approvals.COMMANDS:
        return project_chat_approvals.route(conn, config, argv, request_id)
    if argv == ['bug'] or len(argv) in {2, 3} and argv[1] == 'list':
        result = execute(conn, config, action='list', payload={'after_id': argv[2] if len(argv) == 3 else ''})
        lines = ['已绑定的 Bug（本地记录）']
        for item in result['items'][:8]:
            lines.append(f"{item['title'][:120]}\n查看：bug {item['bug_id']}")
        if not result['items']:
            lines.append('暂无记录。可用 bug import <飞书项目链接> 只读导入。')
        lines.append('新建缺陷：bug create-scope-options 查看可授权范围；bug create-grants 查看已有授权。')
        cursor = result['items'][7]['bug_id'] if len(result['items']) > 8 else result['next_cursor']
        if cursor:
            lines.append('下一页：bug list ' + cursor)
        return {'command': 'project_bug', 'text': '\n\n'.join(lines)}
    if len(argv) in {3, 4} and argv[1] == 'import':
        try:
            hours = int(argv[3]) if len(argv) == 4 else 8
        except ValueError as exc:
            raise ControlError('usage: bug import <url> [read_hours]') from exc
        result = execute(conn, config, action='intake-link', payload={
            'url': argv[2], 'request_id': request_id, 'read_hours': hours, 'local_priority': 'P2'})
        return {'command': 'project_bug', 'text':
                f"只读导入请求：{result['intake_id']}\n状态：{result['state']}\n"
                f"查询进度：bug intake {result['intake_id']}\n不会自动修复或修改远端 Bug。"}
    if len(argv) == 3 and argv[1] == 'intake':
        result = execute(conn, config, action='intake-status', payload={'intake_id': argv[2]})
        text = f"只读导入状态：{result['state']}"
        if result.get('bug_id'):
            text += '\n查看：bug ' + result['bug_id']
        if result.get('error_code'):
            text += '\n阻塞：' + result['error_code']
        return {'command': 'project_bug', 'text': text}
    if len(argv) == 3 and argv[1] == 'progress':
        return _progress(conn, config, argv[2])
    versioned = {'pause', 'resume', 'takeover'}
    communication = {'claim', 'delegate', 'suggest-only'}
    if len(argv) >= 3 and argv[1] in versioned | communication:
        if (argv[1] in versioned and (len(argv) < 4 or not argv[3].isdigit())
                or argv[1] in communication and len(argv) != 3):
            raise ControlError('usage: bug <pause|resume|takeover> <bug_id> <case_version> [reason]')
        detail = execute(conn, config, action='detail', payload={'bug_id': argv[2]})
        return {'delegate_control': shlex.join([argv[1], detail['case_id'], *argv[3:]])}
    if len(argv) == 2:
        detail = execute(conn, config, action='detail', payload={'bug_id': argv[1]})
        case = get_case(conn, detail['case_id'])
        snapshot = detail['snapshot'] or {}
        active = next((r for r in reversed(detail['rounds']) if r['archived_at'] is None), {})
        lifecycle = active.get('lifecycle', {}).get('state', 'current')
        qualifier = '（历史记录）' if lifecycle in {'reopened', 'closed_once', 'archived'} else '（适用性待核对）' if lifecycle == 'unavailable' else ''
        lines = [case['title'][:200], f"Bug：{detail['bug_id']}",
                 f"远端状态（缓存）：{snapshot.get('status_id', '尚未读取')}",
                 f"观测时间：{snapshot.get('observed_at', '无')}",
                 f"本地任务：{case['state']} · 版本 {case['version']}",
                 f"执行：{active.get('execution_state', '尚未开始')}",
                 f"修复{qualifier}：{active.get('repair_state', '尚未确认')}",
                 f"验证{qualifier}：{active.get('verification_state', '尚未验证')}"]
        notice = {
            'reopened': '已观测到 Bug 重新打开；本轮结论仅作历史记录，须开始新轮次重新调查和验证。',
            'closed_once': '本轮已用于关闭缺陷；再次处理须开始新轮次，不能复用本轮关闭证据。',
            'unavailable': '轮次与缺陷流程的对应证据暂不可用，当前修复和验证结论须核对。',
        }.get(lifecycle)
        if notice:
            lines.append(notice)
        if active.get('lifecycle', {}).get('requires_new_round'):
            lines.append(f"新轮次：bug start-round {detail['bug_id']} {detail['revision']} \"重新调查原因\"")
        jobs = detail.get('investigation_jobs') or []
        if jobs:
            count = f"至少 {len(jobs)}" if len(jobs) >= 100 else str(len(jobs))
            lines.append(f"编码调查任务：已记录 {count} 个，显示最新 {min(3, len(jobs))} 个")
            for job in jobs[:3]:
                tool = _CHAT_JOB_TOOLS.get(job.get('agent'), '其他工具')
                state = _CHAT_JOB_STATES.get(job.get('state'), '状态待核对')
                repositories = '、'.join(job.get('repositories') or []) or '仓库待核对'
                predecessor = (f" · 接续 {job['predecessor_job_id']}"
                               if job.get('predecessor_job_id') else '')
                lines.append(f"{job.get('job_id', '任务编号未知')} · 回合 {job.get('round_id', '未知')} "
                             f"· {repositories} · {tool} · {state}{predecessor}")
        else:
            lines.append('编码调查任务：尚未提交')
        unsettled = [o for o in detail['operations'] if o['state'] in {'prepared', 'dispatched', 'unknown'}]
        if unsettled:
            lines.append('待处理写入：' + '、'.join(o['action'] + '/' + o['state'] for o in unsettled))
        lines.extend([f"暂停：bug pause {detail['bug_id']} {case['version']}",
                      f"任务进度：bug progress {detail['bug_id']}",
                      f"接管：bug takeover {detail['bug_id']} {case['version']}",
                      f"我来回复：bug claim {detail['bug_id']}",
                      f"提交调查任务：bug coding-options {detail['bug_id']}",
                      f"授权选项：bug grant-options {detail['bug_id']}",
                      f"查看授权：bug grants {detail['bug_id']}",
                      f"查看写回操作：bug writes {detail['bug_id']}",
                      f"查看关闭审批：bug approvals {detail['bug_id']}",
                      '执行成功、修复完成、验证通过和远端关闭是独立状态。'])
        return {'command': 'project_bug', 'text': '\n'.join(lines)}
    raise ControlError('usage: bug [list|<bug_id>|progress <bug_id>|import <url>|intake <id>]')
