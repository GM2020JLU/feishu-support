"""Generate a professional captured draft from explicitly authored claims."""

import json

import yaml

from .docling_evidence import import_document
from .ids import digest
from .professional_knowledge import validate_article


def build_draft(
    *,
    evidence: dict,
    title: str,
    question: str,
    answer: str,
    references: list[str],
    risk_class: str,
    rollback: str = "",
    authored_scope: dict | None = None,
) -> dict:
    if authored_scope is not None:
        if (not isinstance(authored_scope, dict) or set(authored_scope) != {'product', 'component', 'software_version'}
                or any(not isinstance(v, str) or len(v) > 200 for v in authored_scope.values())):
            raise ValueError('适用范围必须包含产品、组件和软件版本，且每项不超过 200 字符')
    for value, limit in ((title, 200), (question, 2000), (answer, 16000)):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError("标题、问题和候选答案必须明确填写且不超过长度限制")
    if not isinstance(risk_class, str) or risk_class not in {
        "read_only",
        "transient",
        "persistent",
        "destructive",
    }:
        raise ValueError("必须明确选择操作风险")
    if not isinstance(rollback, str) or len(rollback) > 4000:
        raise ValueError("回退说明格式或长度不合法")
    if risk_class in {"persistent", "destructive"} and not rollback.strip():
        raise ValueError("持久化或破坏性操作必须填写回退说明")
    if (
        not isinstance(references, list)
        or not 1 <= len(references) <= 20
        or any(not isinstance(r, str) for r in references)
        or len(set(references)) != len(references)
    ):
        raise ValueError("请选择 1 至 20 个不同的来源位置")
    if not isinstance(evidence, dict):
        raise ValueError("缺少解析材料")  # noqa: TRY004 -- external document validation
    source, parser = evidence.get("source"), evidence.get("parser")
    if not isinstance(source, dict) or not isinstance(parser, dict):
        raise ValueError("缺少来源元数据")  # noqa: TRY004 -- external document validation
    checked = import_document(
        json.dumps(evidence.get("document"), allow_nan=False).encode(),
        source_id=source.get("id"),
        source_version=source.get("version"),
        source_sha256=source.get("sha256"),
        parser_version=parser.get("version"),
        model_revision=parser.get("model_revision"),
    )
    allowed = checked["reading_order"]["body"] + checked["reading_order"]["furniture"]
    if any(ref not in allowed for ref in references):
        raise ValueError("来源位置不属于当前解析材料")
    document_digest = digest(checked["document"])
    stable_id = (
        "attachment."
        + digest({"source": checked["source"], "title": title, "question": question})[
            :24
        ]
    )
    sources = []
    for index, ref in enumerate(references):
        _, collection, item_index = ref.split("/")
        item = checked["document"][collection][int(item_index)]
        sources.append(
            {
                "id": f"attachment-{index}",
                "type": "attachment_derived",
                "stable_external_id": "derived:" + document_digest,
                "title": title,
                "url": None,
                "version": checked["source"]["version"],
                "snapshot_digest": document_digest,
                "authority": 0.0,
                "visibility": "private",
                "share_mode": "never",
                "locator": {
                    "original_source": checked["source"],
                    "parser": checked["parser"],
                    "json_pointer": ref,
                    "item_digest": digest(item),
                    "provenance": item.get("prov", []),
                    "source_verified": False,
                },
            }
        )
    metadata = {
        "schema_version": 2,
        "id": stable_id,
        "revision": 1,
        "kind": "unclassified",
        "status": "captured",
        "title": title,
        "owner": None,
        "scope": {
            "product": "unresolved",
            "component": "unclassified",
            "subcomponent": None,
            "basis": "unresolved",
            "boards": [],
            "hardware_revisions": [],
            "software_versions": ["unresolved"],
            "boot_stages": [],
            "storage_media": [],
            "operating_systems": [],
        },
        "intent": {
            "aliases": [question],
            "question_examples": [question],
            "required_entities": [],
            "negative_constraints": [],
        },
        "content": {
            "summary": answer,
            "prerequisites": [],
            "steps": [],
            "expected_observations": [],
            "failure_branches": [],
            "rollback": [rollback] if rollback.strip() else [],
            "warnings": ["附件解析仅为派生材料；来源、适用范围、声明及风险尚未审核。"],
            "commands": [],
        },
        "claims": [
            {
                "id": "authored-claim",
                "statement": answer,
                "source_refs": [s["id"] for s in sources],
                "risk_class": risk_class,
                "required_validation": ["static"],
            }
        ],
        "sources": sources,
        "validation": [],
        "publication": {
            "answer_visibility": "private",
            "source_body_visibility": "private",
            "link_policy": "never",
            "automatic_reply": False,
            "allowed_chat_ids": [],
            "allowed_user_ids": [],
        },
        "quality": None,
        "review": None,
    }
    if authored_scope is not None:
        for field in ('product', 'component'):
            if authored_scope[field].strip():
                metadata['scope'][field] = authored_scope[field].strip()
        if authored_scope['software_version'].strip():
            metadata['scope']['software_versions'] = [authored_scope['software_version'].strip()]
    revision = validate_article(metadata, answer)
    markdown = (
        "---\n"
        + yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True)
        + "---\n\n"
        + answer
        + "\n"
    )
    return {
        "read_only": True,
        "status": "captured",
        "revision_digest": revision,
        "metadata": metadata,
        "body_markdown": answer,
        "markdown": markdown,
        "filename": stable_id + "-r1.md",
        "notice": "仅生成专业知识草稿；未保存、审核或发布。需补齐来源核验、适用范围和评测。",
    }
