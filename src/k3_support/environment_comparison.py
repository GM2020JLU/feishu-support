"""Read-only comparison of source-bound statements, never physical verification."""

from .context_facts import CONTEXT_FACT_POLICY
from .conversation_context import context_snapshot

FIELDS = {
    "software_version": "软件版本",
    "boot_stage": "启动阶段",
    "storage_medium": "存储介质",
}


def compare_statements(facts):
    rows = []
    if facts.get("policy") != CONTEXT_FACT_POLICY:
        facts = {}
    mentions = facts.get("mentions", [])
    for field, label in FIELDS.items():
        sides = []
        for subject in ("case_site", "test_board:board1"):
            candidates = [
                item
                for index, item in enumerate(mentions)
                if item.get("field") == field
                and item.get("subject") == subject
                and item.get("role") == "observed"
                and item.get("source", {}).get("kind") == "context_event"
                and item.get("source", {}).get("event_digest")
                and (
                    subject != "case_site"
                    or index
                    in facts.get("fields", {})
                    .get(field, {})
                    .get("current_mention_indexes", [])
                )
            ]
            values = {
                item["value"] for item in candidates if item["status"] == "affirmed"
            }
            rejected = {
                item["value"] for item in candidates if item["status"] == "negated"
            }
            state = (
                "conflict"
                if len(values) > 1 or values & rejected
                else "known"
                if values
                else "unknown"
            )
            sides.append(
                {
                    "state": state,
                    "value": next(iter(values)) if state == "known" else None,
                    "sources": [item["source"] for item in candidates],
                }
            )
        left, right = sides
        result = "unknown"
        if "conflict" in (left["state"], right["state"]):
            result = "conflict"
        elif left["state"] == right["state"] == "known":
            result = (
                "same_statement"
                if left["value"] == right["value"]
                else "different_statement"
            )
        rows.append(
            {
                "field": field,
                "label": label,
                "site": left,
                "board1": right,
                "comparison": result,
            }
        )
    return rows


def compare_version_observations(facts, observations):
    """Compare explicit site statements with component-qualified historical logs."""
    if facts.get('policy') != CONTEXT_FACT_POLICY:
        return []
    current = facts.get('fields', {}).get('software_version', {}).get('current_mention_indexes', [])
    mentions = facts.get('mentions', [])
    result = []
    for observed in observations:
        candidates = [item for i, item in enumerate(mentions) if i in current
                      and item.get('subject') == 'case_site'
                      and item.get('role') == 'observed'
                      and item.get('version_component') == observed['component']
                      and item.get('source', {}).get('event_digest')]
        positive = {item['value'] for item in candidates if item['status'] == 'affirmed'}
        negative = {item['value'] for item in candidates if item['status'] == 'negated'}
        state = 'unknown'
        if len(positive) > 1 or positive & negative or observed['multiple_versions']:
            state = 'conflict'
        elif len(positive) == 1:
            state = 'same_version_text' if next(iter(positive)) == observed['version'].lower() else 'different_version_text'
        result.append({**observed, 'comparison': state,
                       'site_version': next(iter(positive)) if len(positive) == 1 else None})
    return result


def comparison_lines(conn, *, case_id, lifecycle_round):
    lines = [
        "",
        "现场 / board1 环境陈述对比",
        "仅比较有来源的当前陈述，不是设备实测；测试板正常不代表现场解决。",
    ]
    row = conn.execute(
        "SELECT context_id FROM conversation_contexts WHERE case_id=? AND lifecycle_round=? AND state<>'retired'",
        (case_id, lifecycle_round),
    ).fetchone()
    if row is None:
        return lines + ["缺少本轮来源绑定上下文，环境差异未知。"]
    snapshot = context_snapshot(conn, row[0])
    if (
        snapshot["state"] != "ready"
        or snapshot["revision"] != snapshot["projected_revision"]
    ):
        return lines + ["上下文尚未就绪或来源已变化，请先核对消息；不展示过时对比。"]
    labels = {
        "unknown": "未知",
        "conflict": "陈述冲突",
        "same_statement": "陈述一致（非实测一致）",
        "different_statement": "陈述不同（非故障原因）",
    }
    for item in compare_statements(snapshot["facts"]):
        lines.append(
            f"{item['label']}：现场={item['site']['value'] or '未知'}；board1={item['board1']['value'] or '未知'}；{labels[item['comparison']]}"
        )
        for key in ("site", "board1"):
            for source in item[key]["sources"]:
                lines.append(
                    f"  {key} 来源：{source.get('message_id')} · {source['event_digest']} · {source.get('verification')}"
                )
    from .board_test_evidence import reviewed_versions
    observations = reviewed_versions(conn, case_id=case_id, lifecycle_round=lifecycle_round)
    comparisons = compare_version_observations(snapshot['facts'], observations)
    if comparisons:
        lines.append('现场版本陈述 / 历史审核串口版本（不证明环境整体一致或故障已修复）')
        states = {'unknown':'无法判断', 'conflict':'版本冲突',
                  'same_version_text':'版本文字一致', 'different_version_text':'版本文字不同，非根因结论'}
        for item in comparisons:
            lines.append(f"{item['component']}：现场={item['site_version'] or '未知'}；"
                         f"历史观测={item['version']} · {item['observed_at']} · {states[item['comparison']]} · "
                         f"证据 {item['evidence_id']}")
    return lines
