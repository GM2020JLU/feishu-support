"""One bounded, session-private preview; never starts workflow consumers."""

import copy
import json
import threading
import uuid

from .ids import digest


def _draft_previews(pipeline):
    previews = []
    for number, turn in enumerate(pipeline.get('turns', [pipeline]), 1):
        for row in turn['intentions']['outbox']:
            if row['channel'] != 'feishu_im' or row['action_type'] not in {'reply', 'clarify'}:
                continue
            payload = json.loads(row['payload_json'])
            text = payload.get('text')
            if isinstance(text, str):
                previews.append({'turn': number, 'kind': row['action_type'],
                    'text': text[:4000], 'truncated': len(text) > 4000})
    return {'items': previews[:20], 'truncated': len(previews) > 20,
            'scope': 'per_turn_draft_only_later_turns_may_invalidate', 'sent': False}


def infer(config, event, *, assumptions=None, documents=None, review_clarification=False, debug=None, followups=None, include_drafts=False):
    from .replay_cli import summary, pipeline_summary
    from .replay_history import run_snapshot_inference
    from .semantic import hermes_message_router

    if debug is not None:
        from .replay_debug_snapshot import run
        from .semantic import _hermes_json

        def reviewer(value):
            answer = _hermes_json(value['prompt'], reasoning='medium', timeout=45)
            return json.dumps(answer) if answer is not None else None

        value = run(config.database_path, {'config': copy.deepcopy(config.raw), **debug}, reviewer=reviewer)
        result = value['result']
        simulated = 'worker' in result
        captured = result.get('review') if simulated else result
        return {'preview_kind': 'simulated_debug_execution' if simulated else 'captured_debug_review', 'scope': value['scope'],
            'completion_state': (result.get('completion') or {}).get('state'),
            'review_ok': captured['review']['ok'] if captured else False,
            'case_state': captured['case']['state'] if captured else None,
            'outbox_intentions': len(result['outbox_intentions']), 'transcript': value['transcript'],
            'calls': value['calls'], 'model_invoked': None,
            'model_callback_invoked': bool(value['calls']), 'external_consumers': False,
            'content_included': False, 'provider_verification': value['provider_verification']}

    if documents is not None:
        from .replay_model_pipeline import run
        from .semantic import hermes_research_link_selector, hermes_clarification_reviewer
        conversation = {'event': event}
        if followups:
            events = [copy.deepcopy(event)]
            for index, text in enumerate(followups, 1):
                incoming = copy.deepcopy(event)
                incoming['external_id'] = f"{event['external_id']}_turn_{index}"
                incoming['payload'] = {**incoming['payload'], 'content': text,
                                       'parent_id': event['external_id']}
                events.append(incoming)
            conversation = {'events': events}
        value = run(config.database_path, {'config': copy.deepcopy(config.raw), **conversation,
                    'assumptions': assumptions or {}, 'documents': documents},
                    router=lambda value: hermes_message_router(value, timeout=45),
                    selector=lambda value: hermes_research_link_selector(value, timeout=45),
                    clarification_reviewer=(lambda value: hermes_clarification_reviewer(value, timeout=45))
                        if review_clarification else None)
        pipeline = value['result']
        if followups:
            result = {'preview_kind': 'conversation', **pipeline_summary(value)}
            if include_drafts:
                result.update(drafts=_draft_previews(pipeline), content_included=True)
            return result
        stages = [pipeline['inbound']]
        if pipeline['research'] is not None:
            stages.append({'research': pipeline['research']})
        result = summary({'steps': stages, 'scope': value['scope'],
                          'knowledge_scope': 'current_snapshot_and_supplied_document_fixtures',
                          'profile_scope': 'current_snapshot_with_explicit_assumptions'})
        result.update(model_invoked=None, model_callback_invoked=bool(value['calls']),
                      assumptions=value['assumptions'], calls=value['calls'],
                      preview_kind='routing_research_clarification' if review_clarification else 'routing_and_document_selection', external_consumers=False,
                      intention_counts={key: len(rows) for key, rows in pipeline['intentions'].items()},
                      provider_verification=value['provider_verification'])
        if include_drafts:
            result.update(drafts=_draft_previews(pipeline), content_included=True)
        return result

    return summary(run_snapshot_inference(
        config.database_path, {'config': copy.deepcopy(config.raw), 'event': event, 'assumptions': assumptions or {}},
        router=lambda value: hermes_message_router(value, timeout=45), timeout=30))


class PreviewTasks:
    """No disk retention, no retries, and at most one running provider request.

    Full results have an evictable 128-item cache; per-session receipts retain
    deduplication up to a hard 512-item limit. Restart is not a safe retry signal.
    """

    def __init__(self, config, runner=None, *, require_session_registration=False):
        self.config = config
        self.runner = runner or infer
        self.lock = threading.Lock()
        self.tasks = {}
        self.receipts = {}
        self.active_sessions = {}
        self.require_session_registration = require_session_registration

    def activate_session(self, session, *, expires_at=float('inf')):
        with self.lock:
            self.active_sessions[session] = expires_at

    def revoke_session(self, session):
        with self.lock:
            self.active_sessions.pop(session, None)
            self._purge_session(session)

    def _purge_session(self, session):
        for key in list(self.receipts):
            if key[0] == session and self.receipts[key]['state'] != 'running':
                self.tasks.pop(key, None)
                self.receipts.pop(key, None)

    def _allowed(self, session):
        if self.require_session_registration and not self._session_active(session):
            self.active_sessions.pop(session, None)
            self._purge_session(session)
            raise ValueError('预演会话已注销或过期；不能读取或新增调用')

    def _session_active(self, session):
        import time
        return self.active_sessions.get(session, 0) > time.monotonic()

    def _remember(self, key, task):
        self.receipts[key] = {name: copy.deepcopy(value) for name, value in task.items()
                              if name in ('request_id', 'digest', 'state', 'execution_state', 'error')}

    def _evict(self):
        while len(self.tasks) >= 128:
            key = next((key for key, task in self.tasks.items() if task['state'] != 'running'), None)
            if key is None:
                raise ValueError('预演正在执行，暂不能清理结果')
            self.tasks.pop(key)
            self.receipts[key]['result_evicted'] = True

    def start(self, session, payload):
        if not isinstance(payload, dict) or set(payload) - {'assumptions', 'documents', 'review_clarification', 'debug', 'followups', 'include_drafts'} != {'request_id', 'event', 'confirm_model_call'}:
            raise ValueError('需要事件、请求标识和模型调用确认')
        from .replay_assumptions import validate
        assumptions = validate(payload.get('assumptions', {}))
        options = {'assumptions': assumptions} if assumptions else {}
        if 'include_drafts' in payload:
            if (type(payload['include_drafts']) is not bool or 'debug' in payload
                    or 'documents' not in payload):
                raise ValueError('拟回复查看必须明确选择资料预演，不能混入 Debug 输入')
            options['include_drafts'] = payload['include_drafts']
        if 'followups' in payload:
            followups = payload['followups']
            if ('debug' in payload or 'documents' not in payload
                    or not isinstance(followups, list) or not 1 <= len(followups) <= 9
                    or any(not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000 for text in followups)):
                raise ValueError('多轮预演需要模拟检索资料和 1 至 9 条后续来信，每条最多 2000 字')
            options['followups'] = copy.deepcopy(followups)
        if 'debug' in payload:
            debug = payload['debug']
            if (payload['event'] or assumptions or 'documents' in payload or 'review_clarification' in payload
                    or not isinstance(debug, dict) or set(debug) - {'execution'} != {'job_id', 'transcript'}
                    or not isinstance(debug['job_id'], str) or not 1 <= len(debug['job_id']) <= 256):
                raise ValueError('Debug 结果预演只能提供任务 ID 和验证记录，不能混入路由输入')
            from .replay_transcript import VerificationTranscript
            VerificationTranscript(debug['transcript'])
            if 'execution' in debug:
                from .replay_debug_snapshot import validate_execution
                validate_execution(debug['execution'])
            options['debug'] = copy.deepcopy(debug)
        if 'review_clarification' in payload:
            if type(payload['review_clarification']) is not bool or ('documents' not in payload and payload['review_clarification']):
                raise ValueError('追问审核必须明确选择，并提供模拟检索资料')
            options['review_clarification'] = payload['review_clarification']
        if 'documents' in payload:
            from .replay_research import validate_documents
            validate_documents(payload['documents'])
            options['documents'] = copy.deepcopy(payload['documents'])
        if payload['confirm_model_call'] is not True or not isinstance(payload['event'], dict):
            raise ValueError('必须明确确认发送路由上下文至已配置模型，可能产生费用')
        identifier = payload['request_id']
        if not isinstance(identifier, str) or str(uuid.UUID(identifier)) != identifier:
            raise ValueError('无效预演请求标识')
        from .ids import canonical_json
        if len(canonical_json(payload).encode()) > 65536:
            raise ValueError('预演事件过大')
        fingerprint = digest(payload)
        with self.lock:
            self._allowed(session)
            previous = self.receipts.get((session, identifier))
            if previous and previous['request_id'] == identifier:
                if previous['digest'] != fingerprint:
                    raise ValueError('同一请求标识不能更改预演内容')
                return self._public(self.tasks.get((session, identifier), previous))
            if any(task['state'] == 'running' for task in self.tasks.values()):
                raise ValueError('已有模型预演进行中，请等待完成')
            if sum(key[0] == session for key in self.receipts) >= 512:
                raise ValueError('当前会话已达 512 条去重收据上限，未启动新调用；重新登录不代表旧调用可安全重试')
            self._evict()
            key = (session, identifier)
            task = {'request_id': identifier, 'digest': fingerprint, 'state': 'running', 'execution_state': 'running'}
            self.tasks[key] = task
            self._remember(key, task)
            event = copy.deepcopy(payload['event'])
            thread = threading.Thread(target=self._run, args=(key, task, event, options), daemon=True)
            try:
                thread.start()
            except Exception:
                task.update(state='failed', execution_state='not_started', error='预演未启动；请核对后另行操作')
                self._remember(key, task)
            return self._public(task)

    def submit(self, session, payload):
        """Distinguish a new rejected request from an existing uncertain call."""
        try:
            return self.start(session, payload)
        except ValueError as error:
            identifier = payload.get('request_id') if isinstance(payload, dict) else None
            with self.lock:
                if isinstance(identifier, str) and (session, identifier) in self.receipts:
                    # A mismatched replay must not imply the earlier call never ran.
                    raise
            return {'state': 'rejected', 'execution_state': 'not_started',
                    'error': str(error)[:240]}

    def _run(self, key, task, event, options):
        try:
            result = self.runner(self.config, event, **options)
        except Exception:
            with self.lock:
                task.update(state='failed', execution_state='unknown', error='预演失败，模型调用结果或费用可能未知；不会自动重试')
                self._remember(key, task)
                if self.require_session_registration and not self._session_active(key[0]):
                    self._purge_session(key[0])
        else:
            with self.lock:
                task.update(state='completed', execution_state='completed', result=result)
                self._remember(key, task)
                if self.require_session_registration and not self._session_active(key[0]):
                    self._purge_session(key[0])

    def status(self, session, identifier):
        with self.lock:
            self._allowed(session)
            task = self.tasks.get((session, identifier), self.receipts.get((session, identifier)))
            if not task or task['request_id'] != identifier:
                raise ValueError('未找到当前会话的预演；控制台重启后结果不可恢复，请勿据此自动重试')
            return self._public(task)

    @staticmethod
    def _public(task):
        return copy.deepcopy({key: value for key, value in task.items() if key != 'digest'})
