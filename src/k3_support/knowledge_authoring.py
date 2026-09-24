"""Durable captured drafts, deliberately outside published knowledge projections."""

import hashlib
import json
import os
import re
from pathlib import Path

from .db import transaction
from .docling_draft import build_draft
from .ids import canonical_json, digest
from .timeutil import iso_now


def save_attachment(conn, *, fields, expected_digest, actor_id):
    if not isinstance(actor_id, str) or not 1 <= len(actor_id.strip()) <= 256:
        raise ValueError("缺少有效控制者身份")
    if not isinstance(fields, dict) or set(fields) - {'authored_scope'} != {
        "evidence",
        "title",
        "question",
        "answer",
        "references",
        "risk_class",
        "rollback",
    }:
        raise ValueError("草稿字段不完整或包含未知字段")
    # Rebuild from authoring inputs, never accept a client-provided approved
    # status, parser verification flag, Markdown frontmatter or review record.
    draft = build_draft(**fields)
    if expected_digest != draft["revision_digest"]:
        raise ValueError("草稿已改变，请重新生成并检查后保存")
    metadata = draft["metadata"]
    locator = metadata["sources"][0]["locator"]
    material = {
        "document": fields["evidence"]["document"],
        "source": locator["original_source"],
        "parser": locator["parser"],
        "status": "unreviewed",
        "automatic_reply_eligible": False,
    }
    encoded = canonical_json(material)
    if len(encoded.encode()) + len(draft["markdown"].encode()) > 2_000_000:
        raise ValueError("草稿和来源材料过大，请拆分后保存")
    candidate_id = "kcd_" + draft["revision_digest"]
    with transaction(conn):
        existing = conn.execute(
            "SELECT * FROM knowledge_authoring_drafts WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO knowledge_authoring_drafts VALUES(?,?,?,?,?,?,?,?)",
                (
                    candidate_id,
                    draft["revision_digest"],
                    metadata["title"],
                    canonical_json(metadata),
                    draft["markdown"],
                    encoded,
                    actor_id,
                    iso_now(),
                ),
            )
        # Deduplication is successful only if the stored artifact is still the
        # same readable candidate. Never hide corruption or overwrite it.
        detail(conn, candidate_id=candidate_id)
        row = conn.execute(
            "SELECT candidate_id,title,saved_at FROM knowledge_authoring_drafts WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
    return {
        **dict(row),
        "replayed": existing is not None,
        "status": "captured",
        "automatic_reply_eligible": False,
        "reviewed": False,
    }


def page(conn, *, after_id=""):
    if not isinstance(after_id, str) or (
        after_id and not re.fullmatch(r"kcd_[0-9a-f]{64}", after_id)
    ):
        raise ValueError("无效草稿分页位置")
    rows = conn.execute(
        "SELECT candidate_id,title,saved_at FROM knowledge_authoring_drafts WHERE candidate_id>? ORDER BY candidate_id LIMIT 21",
        (after_id,),
    ).fetchall()
    return {
        "items": [dict(row) for row in rows[:20]],
        "next_cursor": rows[19]["candidate_id"] if len(rows) > 20 else None,
        "read_only": True,
        "status": "captured",
    }


def detail(conn, *, candidate_id):
    if not isinstance(candidate_id, str) or not re.fullmatch(
        r"kcd_[0-9a-f]{64}", candidate_id
    ):
        raise ValueError("无效草稿标识")
    row = conn.execute(
        "SELECT * FROM knowledge_authoring_drafts WHERE candidate_id=?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise ValueError("未找到已保存草稿")
    metadata = json.loads(row['metadata_json'])
    from .professional_knowledge import parse_article_text, validate_article

    parsed, body = parse_article_text(row['markdown'])
    calculated = validate_article(parsed, body)
    if (parsed != metadata or calculated != row['revision_digest']
            or candidate_id != 'kcd_' + calculated or row['title'] != parsed['title']):
        raise ValueError('stored draft content or revision is inconsistent')
    if parsed.get('status') != 'captured' or parsed.get('review') is not None:
        raise ValueError('only unreviewed captured drafts may be read here')
    material = json.loads(row['material_json'])
    _validate_material(metadata, material)
    return {
        "candidate_id": row["candidate_id"],
        "title": row["title"],
        "revision_digest": row["revision_digest"],
        "markdown": row["markdown"],
        "metadata": metadata,
        "review_tasks": review_tasks(metadata),
        "material": material,
        "saved_by": row["saved_by"],
        "saved_at": row["saved_at"],
        "status": "captured",
        "reviewed": False,
        "automatic_reply_eligible": False,
        "read_only": True,
    }


def _validate_material(metadata, material):
    """Bind displayed derivation to the article, without certifying its source."""
    from .docling_evidence import import_document

    try:
        if (not isinstance(material, dict) or set(material) != {
                'document', 'source', 'parser', 'status', 'automatic_reply_eligible'}):
            raise ValueError('unexpected material fields')
        source, parser = material['source'], material['parser']
        checked = import_document(
            json.dumps(material['document'], allow_nan=False).encode(),
            source_id=source['id'], source_version=source['version'],
            source_sha256=source['sha256'], parser_version=parser['version'],
            model_revision=parser['model_revision'])
        document = checked['document']
        if source != checked['source'] or parser != checked['parser']:
            raise ValueError('displayed source or parser authority mismatch')
        document_digest = digest(document)
        allowed = checked['reading_order']['body'] + checked['reading_order']['furniture']
        if material['status'] != 'unreviewed' or material['automatic_reply_eligible'] is not False:
            raise ValueError('material authority mismatch')
        for entry in metadata['sources']:
            locator = entry['locator']
            pointer = locator['json_pointer']
            if pointer not in allowed:
                raise ValueError('missing source position')
            _, collection, index = pointer.split('/')
            item = document[collection][int(index)]
            if (entry['snapshot_digest'] != document_digest
                    or locator['original_source'] != checked['source']
                    or locator['parser'] != checked['parser']
                    or locator['item_digest'] != digest(item)
                    or locator['provenance'] != item.get('prov', [])):
                raise ValueError('source material mismatch')
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise ValueError('stored draft material is inconsistent') from exc


def review_tasks(metadata):
    """Authoring guidance, never a substitute for the publication validator."""
    tasks = []
    if metadata.get('kind') == 'unclassified':
        tasks.append('classify_knowledge')
    if not metadata.get('owner'):
        tasks.append('assign_owner')
    scope = metadata.get('scope', {})
    if (scope.get('basis') == 'unresolved' or scope.get('product') == 'unresolved'
            or scope.get('component') == 'unclassified' or 'unresolved' in scope.get('software_versions', [])):
        tasks.append('verify_applicability')
    if any(source.get('locator', {}).get('source_verified') is not True for source in metadata.get('sources', [])):
        tasks.append('verify_original_sources')
    passed = {(claim, item['layer']) for item in metadata.get('validation', [])
              if item.get('result') == 'passed' for claim in item.get('claim_refs', [])}
    if any((claim['id'], layer) not in passed for claim in metadata.get('claims', [])
           for layer in claim.get('required_validation', [])):
        tasks.append('validate_claims')
    # Draft checklist always requires a deliberate disclosure and human review;
    # filling fields must not make this captured-artifact endpoint a release gate.
    tasks.extend(['review_disclosure', 'human_review', 'evaluate_before_auto_reply'])
    return {'items': tasks, 'scope': 'authoring_guidance_only', 'release_ready': False}


def export_markdown(conn, *, candidate_id, output):
    """Export one captured draft without changing review or publication state."""
    value = detail(conn, candidate_id=candidate_id)
    target = Path(output).expanduser().absolute()
    if target.suffix.lower() != '.md' or any(path.is_symlink() for path in (target, *target.parents)):
        raise ValueError('export requires a new Markdown path without symlinks')
    if value['metadata'].get('status') != 'captured' or value['metadata'].get('review') is not None:
        raise ValueError('only unreviewed captured drafts may be exported here')
    payload = value['markdown'].encode('utf-8')
    parent = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(target.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)
    return {'candidate_id': candidate_id, 'output': str(target), 'sha256': hashlib.sha256(payload).hexdigest(),
            'bytes': len(payload), 'reviewed': False, 'automatic_reply_eligible': False,
            'notice': 'Exported unreviewed draft only; source verification, scope and review remain required.'}
