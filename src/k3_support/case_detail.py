"""Operator-only views over existing Case records, with no inferred authority."""

from __future__ import annotations

import html
import json
import re
import sqlite3
from typing import Any

from .board_cleanup_status import lines as cleanup_lines
from .board_test_evidence import lines as board_test_lines
from .case_actions import action_binding
from .case_budget import lines as budget_lines
from .case_remote import lines as remote_lines
from .delivery_attempts import in_flight_deliveries
from .environment_comparison import comparison_lines
from .ids import digest
from .knowledge_preview import _display, _pages
from .workbench import case_workbench_status, waiting_for


def _model_observation_lines(conn, case_id):
    records = conn.execute(
        "SELECT created_at,knowledge_runtime_json FROM route_decisions WHERE case_id=? ORDER BY created_at DESC,route_decision_id DESC LIMIT 3",
        (case_id,),
    ).fetchall()
    if not records:
        return []
    lines = ["", "最近知识匹配模型记录（历史记录，非当前运行保证）"]
    for row in records:
        data = json.loads(row["knowledge_runtime_json"])
        observation = data.get("knowledge_selection_observation") if isinstance(data, dict) else None
        if isinstance(observation, dict) and observation.get("verification") == "bridge_sdk_observation":
            model = str(observation.get("model") or "未知")[:256]
            provider = str(observation.get("provider") or "未知")[:256]
            lines.append(f"{row['created_at']} · SDK 观测模型：{model} · provider：{provider}")
            lines.append("观测信息不证明模型权重或账单，不解除知识发布与回复权限校验。")
        else:
            lines.append(f"{row['created_at']} · 无已记录的 SDK 模型观测；不能按配置推定实际模型。")
    return lines


def case_detail(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    page: int = 1,
    expected_digest: str | None = None,
    origin_cursor: str | None = None,
) -> dict[str, Any]:
    """Return escaped complete pages; the control entry verifies operator IDs."""
    if not re.fullmatch(r"K3-[A-Za-z0-9_-]{1,28}", case_id):
        raise ValueError("invalid workbench Case ID")
    conn.execute("SAVEPOINT case_detail_read")
    try:
        return _case_detail(
            conn,
            case_id=case_id,
            page=page,
            expected_digest=expected_digest,
            origin_cursor=origin_cursor,
        )
    finally:
        conn.execute("RELEASE case_detail_read")


def _case_detail(conn, *, case_id, page, expected_digest, origin_cursor):
    from .workbench_navigation import decode, encode, validate_origin

    origin = validate_origin(conn, origin_cursor or "wb2:open:a")
    navigation = decode(conn, origin)[0]
    row = conn.execute("SELECT case_id FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if row is None:
        raise ValueError("workbench Case not found")
    address = conn.execute(
        "SELECT item_seq FROM workbench_item_keys WHERE entity_kind='case' AND target_key=?",
        (case_id,),
    ).fetchone()
    if address is None or address[0] > navigation.upper:
        raise ValueError(
            "workbench Case is outside this browsing range; refresh the workbench"
        )
    from .content_retirement import retired_detail
    retired = retired_detail(conn, case_id=case_id, origin=origin, page=page, expected_digest=expected_digest)
    if retired is not None:
        return retired
    case = dict(conn.execute('SELECT * FROM cases WHERE case_id=?', (case_id,)).fetchone())
    turns = [
        dict(item)
        for item in conn.execute(
            "SELECT * FROM conversation_turns WHERE case_id=? ORDER BY created_at DESC,rowid DESC",
            (case_id,),
        )
    ]
    turn = turns[0] if turns else {}
    source = conn.execute(
        """SELECT ie.payload_json,ie.external_id FROM case_events ce
             JOIN inbound_events ie ON ie.event_pk=ce.source_event_pk
            WHERE ce.case_id=? ORDER BY ce.sequence LIMIT 1""",
        (case_id,),
    ).fetchone()
    original = json.loads(source["payload_json"]) if source else {}
    latest_source = conn.execute(
        "SELECT external_id,payload_json FROM inbound_events WHERE event_pk=?",
        (turn.get("source_event_pk"),),
    ).fetchone()
    latest = json.loads(latest_source["payload_json"]) if latest_source else {}
    evidence = [
        dict(item)
        for item in conn.execute(
            """SELECT e.evidence_id,e.claim,e.result,e.evidence_layer,e.freshness_at,
                  s.title,s.url,s.source_version,s.metadata_json
             FROM evidence e LEFT JOIN case_sources s ON s.source_id=e.source_id
            WHERE e.case_id=? ORDER BY e.created_at,e.evidence_id""",
            (case_id,),
        )
    ]
    reviews = [
        dict(item)
        for item in conn.execute(
            """SELECT status,hermes_output_json,independent_checks_json,updated_at
             FROM codex_reviews WHERE case_id=? ORDER BY updated_at DESC,review_id DESC""",
            (case_id,),
        )
    ]
    approvals = [
        dict(item)
        for item in conn.execute(
            """SELECT approval_type,status,expires_at FROM approvals WHERE case_id=?
             ORDER BY created_at,approval_id""",
            (case_id,),
        )
    ]
    jobs = [
        dict(item)
        for item in conn.execute(
            """SELECT j.job_id,j.job_type,j.state,j.error_class,j.created_at,j.updated_at,
                      json_extract(j.context_json,'$.agent') AS coding_agent,
                      r.received_at AS broker_report_received_at
               FROM jobs j LEFT JOIN broker_results r ON r.job_id=j.job_id
                AND r.attempt_no=j.attempt_no AND r.lifecycle_round=j.lifecycle_round
                AND r.input_digest=j.input_digest
                AND r.lifecycle_round=(SELECT lifecycle_round FROM cases WHERE case_id=j.case_id)
             WHERE j.case_id=? ORDER BY j.created_at,j.job_id""",
            (case_id,),
        )
    ]
    in_flight = in_flight_deliveries(conn, case_id=case_id)
    device_locks = [
        dict(item)
        for item in conn.execute(
            "SELECT lock_key,owner,expires_at FROM locks WHERE case_id=? ORDER BY lock_key",
            (case_id,),
        )
    ]
    handoff = conn.execute(
        """SELECT content_json FROM case_handoffs WHERE case_id=? AND lifecycle_round=?
                              ORDER BY created_at DESC,review_id DESC LIMIT 1""",
        (case_id, case["lifecycle_round"]),
    ).fetchone()
    queue_status = case_workbench_status(conn, case_id=case_id)
    kind = queue_status["kind"]
    lines = [
        f"Case：{case_id}",
        f"标题：{case['title']}",
        f"状态：{case['state']} · {case['severity']} · 版本 {case['version']}",
        f"权限轮次：{case['lifecycle_round']} · 结果来源：{case['outcome_provenance']}",
        f"最近实质进展：{_display(case['last_material_progress_at'])}",
        f"沟通权：{turn.get('communication_owner', '未记录')} / {turn.get('communication_mode', '未记录')}",
        f"等待谁：{waiting_for(case['state'], kind)}",
        f"下一步（记录）：{_display(queue_status['next_action'])}",
        f"问题结果：{_display(case.get('outcome'))}（发送回复不等于现场解决）",
    ]
    if queue_status.get("block_reason"):
        lines.append(f"沟通阻断（记录）：{queue_status['block_reason']}")
    if in_flight:
        lines.append(
            f"已有 {len(in_flight)} 次回复进入发送，结果尚未确认，无法保证撤回"
        )
    for lock in device_locks:
        lines.append(
            f"设备 {lock['lock_key']} 仍有会话占用记录；需先确认安全收尾，标记解决/重开不会释放设备锁。"
        )
    lines.extend(
        [
            "",
            "原始消息（来源数据）",
            f"来源：{_display(original.get('sender_name') or case.get('requester_id'))}",
            f"打开原消息：{_display(original.get('message_app_link'))}",
            _display(original.get("content") or original.get("subject")),
        ]
    )
    if latest_source and (
        not source or latest_source["external_id"] != source["external_id"]
    ):
        lines.extend(
            [
                "",
                "最新已关联消息（来源数据）",
                f"消息：{latest_source['external_id']} · 沟通状态：{turn.get('state', '未记录')}",
                f"打开消息：{_display(latest.get('message_app_link'))}",
                _display(latest.get("content") or latest.get("subject")),
            ]
        )
    lines.extend(
        [
            "",
            "证据范围",
            "以下是已存验证记录；本板正常或未复现，均不能证明对方故障已解决。",
        ]
    )
    if not evidence:
        lines.append("暂无验证证据；不能把调查状态当作测试成功。")
    for item in evidence:
        lines.extend(
            [
                f"[{item['evidence_layer']}] {item['claim']}",
                f"结果：{item['result']} · 时间：{item['freshness_at']}",
                f"来源/版本：{_display(item['title'])} / {_display(item['source_version'])}",
                f"链接：{_display(item['url'])}",
                f"环境/定位（来源记录）：{_display(json.loads(item['metadata_json'] or '{}'))}",
            ]
        )
    lines.extend(comparison_lines(conn, case_id=case_id, lifecycle_round=case["lifecycle_round"]))
    lines.extend(board_test_lines(conn, case_id=case_id, lifecycle_round=case["lifecycle_round"]))
    lines.extend(_model_observation_lines(conn, case_id))
    lines.extend(["", "最近调查审查（判断记录）"])
    if reviews:
        review = reviews[0]
        decision = json.loads(review["hermes_output_json"] or "{}")
        lines.extend(
            [
                f"审查状态：{review['status']}",
                f"已记录事实：{_display(decision.get('facts'))}",
                f"推测：{_display(decision.get('inferences'))}",
                f"尚未确认：{_display(decision.get('unknowns'))}",
                f"独立检查：{_display(json.loads(review['independent_checks_json']))}",
            ]
        )
    else:
        lines.append("暂无审查结论。")
    if handoff:
        content = json.loads(handoff["content_json"])
        lines.extend(["", "本轮调查交接（完整记录）"])
        for key, label in (
            ("facts", "审查事实"),
            ("hypotheses", "假设"),
            ("unconfirmed_differences", "未确认的差异"),
            ("environment_comparison", "环境对比"),
            ("evidence_boundary", "证据边界"),
            ("independent_checks", "独立检查"),
            ("evidence_ids", "证据记录"),
            ("codex_report_untrusted", "Codex 原始报告（来源数据）"),
            ("suggested_next_action", "建议下一步"),
            ("reply_status", "交接时答复状态"),
        ):
            lines.append(f"{label}：{_display(content.get(key))}")
    lines.extend(["", "任务与审批（只读）"])
    for item in jobs:
        if item["broker_report_received_at"] and item["state"] == "running":
            lines.append("代理报告已接收，等待执行退出和结果核验；不代表问题已解决或板卡已释放。")
        agent = item["coding_agent"] if item["job_type"] == "codex" else None
        label = {"codex": "Codex", "claude": "Claude Code", "dsh": "DeepSeek Harness",
                 "opencode": "OpenCode", "hermes": "Hermes"}.get(agent, item["job_type"])
        lines.append(
            f"任务 {label} · {item['job_id']}：{item['state']} · 原因 {_display(item['error_class'])}"
        )
    for item in approvals:
        lines.append(
            f"审批 {item['approval_type']}：{item['status']} · 期限 {item['expires_at']}"
        )
    if not jobs and not approvals:
        lines.append("暂无任务或审批记录。")
    lines.extend(budget_lines(conn, case_id=case_id))
    lines.extend(remote_lines(conn, case_id=case_id))
    lines.extend(cleanup_lines(conn, case_id=case_id))
    text = "\n".join(lines)
    fingerprint = digest(
        {
            "format": 1,
            "case": case,
            "turns": turns,
            "evidence": evidence,
            "reviews": reviews,
            "jobs": jobs,
            "approvals": approvals,
            "in_flight_deliveries": in_flight,
            "device_locks": device_locks,
            "handoff": dict(handoff) if handoff else None,
            "text": text,
        }
    )[:16]
    if expected_digest is not None and fingerprint != expected_digest:
        raise ValueError("workbench detail is stale; refresh the workbench")
    pages = _pages(text)
    if isinstance(page, bool) or not 1 <= page <= len(pages) or len(pages) > 9999:
        raise ValueError("workbench detail page is out of range")
    buttons = []
    for target, label in ((page - 1, "上一页"), (page + 1, "下一页")):
        if 1 <= target <= len(pages):
            buttons.append(
                {
                    "text": label,
                    "callback_data": encode(
                        navigation,
                        item_seq=address[0],
                        page=target,
                        content_digest=fingerprint,
                    ),
                    "row": 0,
                }
            )
    buttons.append({"text": "返回工作台", "callback_data": origin, "row": 1})
    if page == 1:
        from uuid import UUID

        from .broker_remote_state import UNSETTLED
        remote = conn.execute("SELECT a.request_id FROM broker_remote_actions a JOIN broker_grants g USING(grant_id) "
                              "JOIN jobs j USING(job_id) LEFT JOIN broker_remote_results r USING(request_id) "
                              "WHERE j.case_id=? AND a.state='unknown' AND json_valid(a.plan_json) "
                              "AND json_extract(a.plan_json,'$.guard_version')=2 AND " + UNSETTLED +
                              " ORDER BY a.created_at,a.request_id LIMIT 3", (case_id,)).fetchall()
        for index, item in enumerate(remote):
            buttons.append({"text": f"核对远端 {item['request_id'][-8:]}", "callback_data": "rr:q:"+UUID(item["request_id"]).hex,
                            "row": 5+index})
    actions = (
        [("o", "重新打开（人工负责）")]
        if case["state"] in {"resolved", "cancelled"}
        else [("r", "标记解决")]
    )
    if turn and case["state"] not in {"resolved", "cancelled", "takeover"}:
        actions = [("c", "我来回复"), ("s", "只给我建议"), ("a", "交给 AI")] + actions
    if not case.get("canonical_case_id"):
        for index, (code, label) in enumerate(actions):
            action = {
                "r": "resolve",
                "o": "reopen",
                "c": "claim",
                "a": "delegate",
                "s": "suggest_only",
            }[code]
            token = action_binding(conn, case_id=case_id, action=action)["token"]
            callback = f"wka2:{code}:{case_id}:{token}"
            if len(callback.encode()) <= 64:
                buttons.append(
                    {"text": label, "callback_data": callback, "row": 2 + index // 2}
                )
    return {
        "command": "workbench",
        "operation": "detail",
        "case_id": case_id,
        "origin_cursor": origin,
        "preview": {
            "text": f"<b>Case 详情 · {page}/{len(pages)} 页</b>\n只读查看，不改变沟通或执行权。\n\n"
            + pages[page - 1],
            "parse_mode": "HTML",
            "plain_text": html.unescape(pages[page - 1]),
            "buttons": buttons,
            "page": page,
            "page_count": len(pages),
            "content_digest": fingerprint,
        },
    }
