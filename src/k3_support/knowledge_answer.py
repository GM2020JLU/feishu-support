"""Deterministic published-answer rendering shared by planning and dispatch."""

from .professional_knowledge import answer_trust_context


def approved_answer_markdown(conn, knowledge: dict) -> str:
    reply = f"[AI 自动回复]\n\n**答复**\n\n{knowledge['answer_markdown']}"
    trust = answer_trust_context(conn, knowledge_id=str(knowledge["knowledge_id"]))
    if trust is None:
        return reply
    scope = trust["scope"]
    scope_values = [scope["product"], scope["component"]]
    scope_values.extend(scope.get("boards") or [])
    scope_values.extend(scope.get("software_versions") or [])
    lines = [
        "", "**可信范围**", f"- 适用：{' / '.join(scope_values)}",
        f"- 知识版本：`{trust['stable_id']}@r{trust['revision']}`",
        f"- 已验证：{', '.join(trust['validation_layers'])}",
        f"- 审核时间：{str(trust['reviewed_at'])[:10]}",
    ]
    source_links = []
    for source in trust["sources"][:3]:
        title = source["title"] or source["id"]
        source_links.append(f"[{title}]({source['url']})" if source["url"] else str(title))
    if source_links:
        lines.append(f"- 来源：{'；'.join(source_links)}")
    return reply + "\n".join(lines)
