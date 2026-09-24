"""Explicit retirement state for consumers. This module never clears content."""
import html

from .ids import digest


class ContentRetiredError(ValueError):
    pass


def require_case_content(conn, *, case_id, lifecycle_round=None):
    if lifecycle_round is not None and (type(lifecycle_round) is not int or lifecycle_round < 1):
        raise ValueError('invalid content lifecycle round')
    if case_id is None:
        return
    if not isinstance(case_id, str) or not 1 <= len(case_id) <= 128:
        raise ValueError('invalid content Case identity')
    row = conn.execute('SELECT lifecycle_round FROM case_content_retirements WHERE case_id=?'
                       + (' AND lifecycle_round=?' if lifecycle_round is not None else '') + ' LIMIT 1',
                       (case_id, lifecycle_round) if lifecycle_round is not None else (case_id,)).fetchone()
    if row is not None:
        raise ContentRetiredError('该轮案件内容已按保留策略清理，不能重建或回放原文；请提供新的来源资料。')


def require_current_turn_after_retirement(conn, *, case_id, lifecycle_round, turn_id):
    """Legacy turns have no round column; require current-context membership."""
    require_case_content(conn, case_id=case_id, lifecycle_round=lifecycle_round)
    if conn.execute('SELECT 1 FROM case_content_retirements WHERE case_id=? LIMIT 1',
                    (case_id,)).fetchone() is None:
        return
    bound = conn.execute('''SELECT 1 FROM conversation_turns t
        JOIN conversation_context_members m ON m.event_pk=t.source_event_pk
        JOIN conversation_contexts c ON c.context_id=m.context_id
        WHERE t.turn_id=? AND t.case_id=? AND c.case_id=? AND c.lifecycle_round=?
          AND c.state<>'retired' ''', (turn_id,case_id,case_id,lifecycle_round)).fetchone()
    if bound is None:
        raise ContentRetiredError('旧轮次对话不可恢复；请先关联本轮新的消息。')


def _round_operations(conn, case_id, lifecycle_round):
    """Bounded coordination metadata only; never select diagnostic/payload text."""
    result = {}
    for table, identity, kind, state in (
        ('jobs', 'job_id', 'job_type', 'state'),
        ('approvals', 'approval_id', 'approval_type', 'status'),
        ('outbox', 'outbox_id', 'action_type', 'state'),
    ):
        rows = conn.execute(f'''SELECT {identity} AS id,{kind} AS kind,{state} AS state
            FROM {table} WHERE case_id=? AND lifecycle_round=?
            ORDER BY rowid DESC LIMIT 21''', (case_id, lifecycle_round)).fetchall()
        result[table] = {'items': [dict(row) for row in rows[:20]], 'truncated': len(rows) > 20}
    return result


def retired_detail(conn, *, case_id, origin, page, expected_digest):
    receipts = conn.execute('''SELECT lifecycle_round,receipt_id,retired_at
        FROM case_content_retirements WHERE case_id=? ORDER BY lifecycle_round DESC LIMIT 21''',
        (case_id,)).fetchall()
    if not receipts:
        return None
    case = conn.execute('SELECT case_id,state,version,lifecycle_round,canonical_case_id FROM cases WHERE case_id=?',
                        (case_id,)).fetchone()
    metadata = {'case': dict(case), 'receipts': [dict(row) for row in receipts], 'origin': origin}
    current = None
    active_round = not any(row['lifecycle_round'] == case['lifecycle_round'] for row in receipts)
    if active_round:
        require_case_content(conn, case_id=case_id, lifecycle_round=case['lifecycle_round'])
        metadata['operations'] = _round_operations(conn, case_id, case['lifecycle_round'])
        has_board_records = conn.execute('''SELECT 1 FROM broker_grants g
            JOIN jobs j ON j.job_id=g.job_id WHERE j.case_id=?
            AND j.lifecycle_round=? AND g.lifecycle_round=?
            UNION ALL SELECT 1 FROM codex_reviews r JOIN jobs j ON j.job_id=r.job_id
            WHERE r.case_id=? AND j.case_id=? AND j.lifecycle_round=? LIMIT 1''',
            (case_id,case['lifecycle_round'],case['lifecycle_round'],
             case_id,case_id,case['lifecycle_round'])).fetchone()
        if has_board_records:
            from .board_test_evidence import lines as board_lines
            metadata['board_evidence_lines'] = board_lines(
                conn,case_id=case_id,lifecycle_round=case['lifecycle_round'])
        context = conn.execute('''SELECT context_id FROM conversation_contexts
            WHERE case_id=? AND lifecycle_round=? AND state<>'retired' ''',
            (case_id, case['lifecycle_round'])).fetchone()
        if context is not None:
            from .conversation_context import context_snapshot
            current = context_snapshot(conn, context['context_id'])
            metadata['current_context'] = current
    communication_buttons = []
    if (current is not None and current['state'] == 'ready'
            and current['revision'] == current['projected_revision']
            and case['state'] not in {'resolved','cancelled','takeover'}
            and not case['canonical_case_id']):
        from .case_actions import action_binding
        for code, action, label in (('c','claim','我来回复'),
                                    ('s','suggest_only','只给我建议'),
                                    ('a','delegate','交给 AI')):
            try:
                binding = action_binding(conn,case_id=case_id,action=action)
            except ContentRetiredError:
                break  # No current-round turn: never offer the old one.
            callback = f"wka2:{code}:{case_id}:{binding['token']}"
            if len(callback.encode()) <= 64:
                communication_buttons.append({'text':label,'callback_data':callback,'row':2})
    metadata['communication_buttons'] = communication_buttons
    lifecycle_buttons = []
    if not case['canonical_case_id'] and (active_round or case['state'] in {'resolved','cancelled'}):
        from .case_actions import action_binding
        closed = case['state'] in {'resolved','cancelled'}
        action, code, label = ('reopen','o','重新打开（人工负责）') if closed else ('resolve','r','标记解决')
        binding = action_binding(conn,case_id=case_id,action=action)
        callback = f"wka2:{code}:{case_id}:{binding['token']}"
        if len(callback.encode()) <= 64:
            lifecycle_buttons.append({'text':label,'callback_data':callback,'row':3})
    metadata['lifecycle_buttons'] = lifecycle_buttons
    fingerprint = digest(metadata)
    if active_round:
        fingerprint = fingerprint[:16]  # Existing workbench pagination contract.
    if expected_digest is not None and expected_digest != fingerprint:
        raise ValueError('stale Case detail; refresh the workbench')
    if page != 1 and not active_round:
        raise ValueError('retired Case metadata has one page; refresh the workbench')
    lines = [f"案件 {case_id} · 状态 {case['state']} · 当前轮次 {case['lifecycle_round']}",
             ('历史内容已清理，下方仅显示回执及新轮次资料。' if active_round
              else '历史内容已清理，本页仅显示保留元数据。'),
             '不显示旧轮次原文、不恢复旧审批，也不据此判定问题已解决。']
    for row in receipts[:20]:
        lines.append(f"第 {row['lifecycle_round']} 轮 · 清理时间 {row['retired_at']} · 回执 {row['receipt_id']}")
    if len(receipts) > 20:
        lines.append('仅显示最近 20 条清理回执。')
    if current is not None:
        lines.append(f"第 {case['lifecycle_round']} 轮新资料（不包含已清理轮次的详情）")
        if current['state'] == 'ready' and current['revision'] == current['projected_revision']:
            lines.append(current['query'])
        else:
            lines.append('本轮上下文尚未就绪或来源已变化，暂不展示内容。')
        lines.append('这里只读展示新一轮上下文；不恢复旧审批或旧证据。')
    if active_round:
        if current is None:
            lines.append('本轮尚无新上下文；不会从历史消息恢复问题。')
        lines.append('本轮任务、审批和答复状态（只读；成功不代表现场问题已解决）')
        for table, label in (('jobs', '任务'), ('approvals', '审批'), ('outbox', '答复')):
            group = metadata['operations'][table]
            for row in group['items']:
                lines.append(f"{label} {row['id']} · {row['kind']} · {row['state']}")
            if not group['items']:
                lines.append(f'本轮暂无{label}记录。')
            if group['truncated']:
                lines.append(f'{label}仅显示最近 20 条创建记录；不是完整历史。')
    if active_round:
        lines.extend(metadata.get('board_evidence_lines', []))
    plain = '\n'.join(lines)
    if active_round:
        from .knowledge_preview import _pages
        from .workbench_navigation import decode, encode
        pages = _pages(plain)
        if type(page) is not int or not 1 <= page <= len(pages):
            raise ValueError('invalid current-round detail page')
        navigation = decode(conn, origin)[0]
        address = conn.execute("SELECT item_seq FROM workbench_item_keys WHERE entity_kind='case' AND target_key=?",
                               (case_id,)).fetchone()[0]
        buttons = [{'text': label, 'callback_data': encode(navigation, item_seq=address,
                    page=target, content_digest=fingerprint), 'row': 0}
                   for target, label in ((page-1, '上一页'), (page+1, '下一页'))
                   if 1 <= target <= len(pages)]
        buttons.append({'text': '返回工作台', 'callback_data': origin, 'row': 1})
        if page == 1:
            buttons.extend(communication_buttons)
            buttons.extend(lifecycle_buttons)
        return {'command':'workbench', 'operation':'detail', 'case_id':case_id, 'origin_cursor':origin,
                'preview': {'text':pages[page-1], 'plain_text':html.unescape(pages[page-1]),
                            'parse_mode':'HTML', 'buttons':buttons, 'page':page,
                            'page_count':len(pages), 'content_digest':fingerprint,
                            'content_state':'current_context_with_retired_history'}}
    return {'command':'workbench', 'operation':'detail', 'case_id':case_id, 'origin_cursor':origin,
            'preview': {'text':html.escape(plain), 'plain_text':plain, 'parse_mode':'HTML',
                        'buttons':[{'text':'返回工作台', 'callback_data':origin, 'row':0}] + lifecycle_buttons,
                        'page':1, 'page_count':1, 'content_digest':fingerprint,
                        'content_state':'retired_metadata_only'}}
