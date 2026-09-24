"""The shared live/evaluation retrieval path. Inputs contain no expected answers.

SQLite is the authority; optional indexes and model output can only nominate IDs.
This module does not grant publication or automatic-reply authority.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .context_facts import CONTEXT_FACT_POLICY
from .ids import canonical_json, digest
from .inbound_claims import InboundClaimLost
from .scope_facts import (
    SCOPE_FACT_POLICY,
    SCOPE_KEYS,
    ScopeFactError,
    analyze_scope_facts,
)

TOKENIZER_VERSION = "sqlite-fts5-exact-ascii-cjk-bigram-v1"
SELECTION_CONTRACT = "approved-metadata-id-confidence-v3"
SCOPE_POLICY = SCOPE_FACT_POLICY
DEFAULT_OPTIONS = {
    "backend": "sqlite",
    "candidate_limit": 20,
    "prefetch_limit": 80,
    "allow_fallback": True,
    "qdrant_path": None,
    "qdrant_collection": "knowledge",
}
_SCOPE_KEYS = SCOPE_KEYS
_TOKEN_RE = re.compile(
    r"[A-Za-z0-9_]+(?:[./:+-][A-Za-z0-9_]+)*|-[0-9]+|[\u3400-\u9fff]+"
)
_INTERNAL_RELATIONS = {
    "supervisor",
    "dotted_supervisor",
    "peer",
    "direct_report",
    "cross_function",
}
_CONTENT_FIELDS = (
    "knowledge_id",
    "title",
    "status",
    "question_variants_json",
    "answer_markdown",
    "project",
    "module",
    "hardware",
    "software_version",
    "applicability",
    "disclosure_class",
    "allowed_chat_ids_json",
    "allowed_user_ids_json",
    "confidence",
    "source_authority",
    "review_due_at",
    "source_digest",
    "content_digest",
    "professional_revision_id",
    "revision_state",
    "revision_digest",
    "revision_payload",
)


class RetrievalError(ValueError):
    pass


def event_query(event) -> str:
    """One query extraction rule for ingestion, replay and final dispatch."""
    payload = json.loads(event["payload_json"])
    if event["source"] == "feishu_mail":
        return "\n".join(value for value in (
            str(payload.get("subject") or "").strip(),
            str(payload.get("body_preview") or "").strip(),
        ) if value)
    return str(payload.get("content") or payload.get("subject") or "").strip()


def event_input_digest(event) -> str:
    """Exclude worker counters, but bind source, audience and original content."""
    return digest({
        **{key: event[key] for key in (
            "event_pk", "source", "identity", "external_id", "sender_id",
            "chat_id", "thread_id", "occurred_at",
        )},
        "payload": json.loads(event["payload_json"]),
    })


def retrieval_options(options: dict[str, Any] | None = None) -> dict[str, Any]:
    supplied = {} if options is None else options
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT_OPTIONS):
        raise RetrievalError("invalid retrieval options")
    result = {**DEFAULT_OPTIONS, **supplied}
    if result["backend"] not in {"sqlite", "qdrant"}:
        raise RetrievalError("invalid retrieval backend")
    for key, ceiling in (("candidate_limit", 100), ("prefetch_limit", 500)):
        if type(result[key]) is not int or not 1 <= result[key] <= ceiling:
            raise RetrievalError(f"{key} must be an integer in 1..{ceiling}")
    if result["prefetch_limit"] < result["candidate_limit"]:
        raise RetrievalError("prefetch_limit must cover candidate_limit")
    if type(result["allow_fallback"]) is not bool:
        raise RetrievalError("allow_fallback must be boolean")
    if result["qdrant_path"] is not None and (
        not isinstance(result["qdrant_path"], str)
        or not Path(result["qdrant_path"]).is_absolute()
        or ".." in Path(result["qdrant_path"]).parts
    ):
        raise RetrievalError("qdrant_path must be an explicit absolute local path")
    if not isinstance(result["qdrant_collection"], str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", result["qdrant_collection"]
    ):
        raise RetrievalError("invalid qdrant_collection")
    return result


def query_tokens(text: str) -> list[str]:
    """Keep error/command tokens intact and segment continuous Chinese locally."""
    result: list[str] = []
    for token in _TOKEN_RE.findall(text.lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", token):
            result.extend(token[i : i + 2] for i in range(max(1, len(token) - 1)))
        else:
            result.append(token)
    return list(dict.fromkeys(result))


def _json(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else default
    except (ValueError, TypeError):
        return default


def _future(value: Any) -> bool:
    from .timeutil import utc_now

    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.tzinfo is not None and parsed.astimezone(UTC) > utc_now()
    except (TypeError, ValueError):
        return False


def _profile(
    conn, requester_id: str | None, explicit: dict[str, Any] | None
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM requester_profiles WHERE requester_id=?", (requester_id,)
    ).fetchone()
    # A persisted newer/expired profile wins over an earlier invocation snapshot.
    profile = dict(row) if row else dict(explicit or {})
    if profile.get("requester_id") != requester_id:
        return {}
    if profile.get("source") not in {"operator", "feishu_contact"}:
        return {}
    if profile.get("expires_at") and not _future(profile["expires_at"]):
        return {}
    if profile.get("source") == "feishu_contact" and (
        not profile.get("verified_at") or not _future(profile.get("expires_at"))
    ):
        return {}
    return profile


def _acl_allowed(row: dict[str, Any], requester_id, chat_id, profile) -> bool:
    level = row["disclosure_class"]
    if level == "public":
        return True
    users = _json(row["allowed_user_ids_json"], [])
    chats = _json(row["allowed_chat_ids_json"], [])
    explicit = bool(
        (requester_id and requester_id in users) or (chat_id and chat_id in chats)
    )
    if level == "team":
        return bool(chat_id and chat_id in chats)
    if level in {"private", "restricted"}:
        return explicit
    if level == "internal":
        return (
            profile.get("relationship") in _INTERNAL_RELATIONS
            and type(profile.get("relationship_confidence")) in {int, float}
            and 0.85 <= profile["relationship_confidence"] <= 1
        )
    return False


def corpus_rows(conn) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """SELECT ke.*, p.lifecycle_state AS revision_state,
                  p.revision_digest AS revision_digest, p.payload_json AS revision_payload
             FROM knowledge_entries ke LEFT JOIN professional_knowledge_revisions p
               ON p.revision_id=ke.professional_revision_id
            WHERE ke.status='approved' ORDER BY ke.knowledge_id"""
        )
    ]


def entry_fingerprint(row: dict[str, Any]) -> str:
    """Content/authority identity, deliberately independent of usage timestamps."""
    return digest({key: row.get(key) for key in _CONTENT_FIELDS})


def candidate_rows(conn, knowledge_ids) -> list[dict[str, Any]]:
    """Load only bounded candidate bodies; not a corpus digest or ACL decision."""
    if not isinstance(knowledge_ids, (list, tuple)) or len(knowledge_ids) > 500:
        raise RetrievalError('candidate body ID budget exceeded')
    if any(not isinstance(key, str) or not key or len(key) > 200 for key in knowledge_ids):
        raise RetrievalError('invalid candidate body identity')
    keys = sorted(set(knowledge_ids))
    if not keys:
        return []
    placeholders = ','.join('?' for _ in keys)
    return [dict(row) for row in conn.execute(f'''SELECT ke.*,
        p.lifecycle_state AS revision_state,p.revision_digest AS revision_digest,
        p.payload_json AS revision_payload
        FROM knowledge_entries ke LEFT JOIN professional_knowledge_revisions p
        ON p.revision_id=ke.professional_revision_id
        WHERE ke.status='approved' AND ke.knowledge_id IN ({placeholders})
        ORDER BY ke.knowledge_id''', keys)]


def corpus_fingerprint(rows: list[dict[str, Any]]) -> str:
    return digest(sorted((row["knowledge_id"], entry_fingerprint(row)) for row in rows))


def entry_metadata(row: dict[str, Any]) -> dict[str, Any]:
    payload = _json(row.get("revision_payload"), {})
    scope = payload.get("scope", {})
    return {
        "knowledge_id": row["knowledge_id"],
        "title": row["title"],
        "question_examples": _json(row["question_variants_json"], []),
        "project": row["project"],
        "module": row["module"],
        "software_version": row["software_version"],
        "kind": payload.get("kind", "legacy"),
        "scope": scope,
        "required_entities": payload.get("intent", {}).get("required_entities", []),
        "negative_constraints": payload.get("intent", {}).get(
            "negative_constraints", []
        ),
    }


def metadata_text(row: dict[str, Any]) -> str:
    return canonical_json(entry_metadata(row))


def _normal(value: Any) -> str:
    return str(value).strip().lower().replace("_", "-")


def infer_observed_scope(
    query: str, supplied: dict[str, str] | None = None
) -> dict[str, str]:
    """Compatibility projection; callers needing evidence use scope_facts."""
    return _scope_facts(query, supplied)["observed_scope"]


def _scope_facts(query, supplied):
    try:
        return analyze_scope_facts(query, supplied)
    except ScopeFactError as exc:
        raise RetrievalError(str(exc)) from exc


def _input_facts(conn, *, query, observed_scope, context_binding, chat_id):
    if context_binding is None:
        return _scope_facts(query, observed_scope)
    from .conversation_context import context_snapshot, validate_context_binding

    if observed_scope is not None:
        raise RetrievalError('context facts cannot be replaced by caller supplied scope')
    valid, reason = validate_context_binding(conn, context_binding)
    if not valid:
        raise RetrievalError(reason)
    snapshot = context_snapshot(conn, context_binding['context_id'])
    if snapshot['query'] != query or snapshot['chat_id'] != chat_id:
        raise RetrievalError('context query or conversation does not match')
    facts = snapshot['facts']
    if snapshot['facts_digest'] != digest(facts) or facts.get('query_digest') != digest(query):
        raise RetrievalError('context facts projection changed')
    return facts


def _scope_state(row: dict[str, Any], observed: dict[str, str], facts=None) -> str | None:
    meta = entry_metadata(row)
    scope = meta["scope"]
    fields = {
        "product": scope.get("product") or row["project"],
        "component": scope.get("component") or row["module"],
        "board": scope.get("boards", _json(row.get("hardware"), [])),
        "software_version": scope.get("software_versions") or row["software_version"],
        "boot_stage": scope.get("boot_stages", []),
        "storage_medium": scope.get("storage_media", []),
    }
    missing = set()
    for key, declared in fields.items():
        values = (
            declared if isinstance(declared, list) else [declared] if declared else []
        )
        allowed = {_normal(v) for v in values} - {"not-applicable", "all", "any"}
        evidence = (facts or {}).get("fields", {}).get(key, {})
        if allowed and evidence.get("state") == "conflict":
            return "ambiguous_scope"
        if allowed and allowed <= set(evidence.get("excluded_values", [])):
            return "scope_mismatch"
        if key in observed and allowed and _normal(observed[key]) not in allowed:
            return "scope_mismatch"
        if allowed and key not in observed:
            missing.add(key)
    if meta["kind"] == "legacy":
        return (
            None  # Existing records have no machine-readable required-entity contract.
        )
    if scope.get("basis") == "unresolved":
        return "ambiguous_scope"
    # A link-only route is useful without pretending its document's target is known.
    payload = _json(row.get("revision_payload"), {})
    link_only = meta["kind"] == "document_route" and all(
        source.get("share_mode") in {"link_only", "never"}
        for source in payload.get("sources", [])
    )
    if link_only:
        return None
    required = set(meta["required_entities"])
    if required - _SCOPE_KEYS:
        # Free-text prerequisites are not magically resolved by a high score.
        return "ambiguous_scope"
    if scope.get("basis") == "hardware_specific":
        required.add("board")
    if meta["kind"] in {
        "procedure",
        "command_reference",
        "validation_recipe",
        "compatibility_matrix",
    }:
        required.update(missing & {"software_version", "boot_stage", "storage_medium"})
    if "software_version" in required and "software_version" not in observed:
        return "version_unknown"
    if required - set(observed):
        return "ambiguous_scope"
    return None


def eligible_rows(conn, rows, *, requester_id, chat_id, scope, verified_profile=None, facts=None):
    profile = _profile(conn, requester_id, verified_profile)
    eligible, reasons = [], Counter()
    for row in rows:
        if not _acl_allowed(row, requester_id, chat_id, profile):
            reasons["acl_denied"] += 1
            continue
        if (row.get("review_due_at") and not _future(row["review_due_at"])) or (
            row.get("professional_revision_id")
            and row.get("revision_state") != "published"
        ):
            reasons["stale_evidence"] += 1
            continue
        if _scope_state(row, scope, facts) == "scope_mismatch":
            reasons["scope_mismatch"] += 1
            continue
        eligible.append(row)
    return eligible, reasons


def _fts_ranks(conn, rows, query):
    tokens = query_tokens(query)
    # Keep command/error tokens before the bounded CJK expansion.
    tokens.sort(key=lambda token: bool(re.search(r"[\u3400-\u9fff]", token)))
    if not rows or not tokens:
        return {}
    expression = " OR ".join(f'"{token}"' for token in tokens[:128])
    allowed = canonical_json([row["knowledge_id"] for row in rows])
    return {
        row["knowledge_id"]: index
        for index, row in enumerate(
            conn.execute(
                """SELECT knowledge_fts.knowledge_id, bm25(knowledge_fts) AS rank
             FROM knowledge_fts
            WHERE knowledge_fts MATCH ?
              AND knowledge_fts.knowledge_id IN (SELECT value FROM json_each(?))
            ORDER BY rank,knowledge_fts.knowledge_id""",
                (expression, allowed),
            )
        )
    }


def lexical_recall(
    rows: list[dict[str, Any]], query: str, limit: int, *, fts_ranks=None
) -> list[dict[str, Any]]:
    tokens = query_tokens(query)
    if not tokens:
        return []
    scored = []
    documents = [
        (
            row,
            set(query_tokens(metadata_text(row))),
            set(query_tokens(row["answer_markdown"])),
        )
        for row in rows
    ]
    frequency = Counter(
        token for _, meta, body in documents for token in (meta | body) & set(tokens)
    )
    for row, meta, body in documents:
        score = sum(
            math.log(1 + (len(rows) + 1) / (frequency[token] + 1))
            * (4 if token in meta else 1)
            for token in tokens
            if token in meta or token in body
        )
        if score:
            if row["knowledge_id"] in (fts_ranks or {}):
                score += 0.5 / (1 + fts_ranks[row["knowledge_id"]])
            scored.append((score, row["knowledge_id"], row))
    return [row for _, _, row in sorted(scored, key=lambda v: (-v[0], v[1]))[:limit]]


def _public_entry(row):
    return {
        key: value
        for key, value in row.items()
        if key
        not in {
            "allowed_user_ids_json",
            "allowed_chat_ids_json",
            "revision_payload",
            "revision_state",
            "revision_digest",
        }
    }


def load_approved_entry(
    conn,
    *,
    knowledge_id,
    requester_id,
    chat_id,
    query="",
    observed_scope=None,
    verified_profile=None,
    context_binding=None,
):
    facts = _input_facts(conn, query=query, observed_scope=observed_scope,
                         context_binding=context_binding, chat_id=chat_id)
    scope = facts["observed_scope"]
    rows = candidate_rows(conn, [knowledge_id])
    eligible, _ = eligible_rows(
        conn,
        rows,
        requester_id=requester_id,
        chat_id=chat_id,
        scope=scope,
        verified_profile=verified_profile,
        facts=facts,
    )
    if not eligible or _scope_state(eligible[0], scope, facts):
        return None
    row = eligible[0]
    return {**_public_entry(row), "knowledge_entry_fingerprint": entry_fingerprint(row)}


def _selector_identity(selector) -> dict[str, Any]:
    # A callable name/configured model alias is not proof of a model actually used.
    if selector is None:
        return {
            "provider": "none",
            "revision": "semantic-selection-required-v1",
            "verification": "local_code",
        }
    return {
        "provider": "callable",
        "implementation": f"{getattr(selector, '__module__', '')}.{getattr(selector, '__qualname__', type(selector).__name__)}",
        "verification": "unverified",
        "model_revision": None,
    }


def query_knowledge(
    conn: sqlite3.Connection,
    *,
    query: str,
    requester_id: str | None,
    chat_id: str | None,
    observed_scope: dict[str, str] | None = None,
    verified_profile: dict[str, Any] | None = None,
    selector=None,
    options: dict[str, Any] | None = None,
    hybrid=None,
    minimum_confidence: float = 0.85,
    context_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Retrieve/select using actual inputs only; never consume evaluation labels.

    A missing selector retains retrieval candidates but never selects an answer.
    Selection failures abstain. An index fallback changes the binding.
    """
    started = time.monotonic()
    opts = retrieval_options(options)
    if not isinstance(query, str) or len(query) > 32768:
        raise RetrievalError("query must be a string of at most 32768 characters")
    if type(minimum_confidence) not in {int, float} or not 0 <= minimum_confidence <= 1:
        raise RetrievalError("invalid minimum confidence")
    facts = _input_facts(conn, query=query, observed_scope=observed_scope,
                         context_binding=context_binding, chat_id=chat_id)
    scope = facts["observed_scope"]
    generation = None
    recall_issue = None
    recall_filters = {}
    if opts['backend'] == 'sqlite':
        from .knowledge_corpus import state as corpus_state
        from .knowledge_lexical import recall
        generation = corpus_state(conn)
        if not generation['current']:
            rows = []
            recall_issue = 'corpus_not_current'
        else:
            lexical = recall(conn, query=query, requester_id=requester_id, chat_id=chat_id,
                             scope=scope, verified_profile=verified_profile, facts=facts,
                             limit=opts['prefetch_limit'])
            recall_filters = lexical['filter_counts']
            if not lexical['complete']:
                rows = []
                recall_issue = lexical['reason']
            else:
                rows = candidate_rows(conn, [row['knowledge_id'] for row in lexical['items']])
    else:
        rows = corpus_rows(conn)
    full_corpus_digest = generation['corpus_digest'] if generation is not None else corpus_fingerprint(rows)
    eligible, reasons = eligible_rows(
        conn,
        rows,
        requester_id=requester_id,
        chat_id=chat_id,
        scope=scope,
        verified_profile=verified_profile,
        facts=facts,
    )
    reasons.update(recall_filters)
    baseline = lexical_recall(
        eligible,
        query,
        opts["prefetch_limit"],
        fts_ranks=_fts_ranks(conn, eligible, query),
    )
    effective = "sqlite"
    fallback = None
    index_binding = None
    retrieved = baseline
    if opts["backend"] == "qdrant":
        try:
            if hybrid is None:
                raise RetrievalError("hybrid_provider_unavailable")
            if hybrid.collection != opts["qdrant_collection"] or (
                opts["qdrant_path"] is not None
                and hybrid.location != opts["qdrant_path"]
            ):
                raise RetrievalError("hybrid_instance_does_not_match_configuration")
            result = hybrid.retrieve(
                query=query,
                eligible=eligible,
                corpus_digest=corpus_fingerprint(rows),
                limit=opts["prefetch_limit"],
            )
            allowed = {row["knowledge_id"]: row for row in eligible}
            hybrid_ids = list(
                dict.fromkeys(key for key in result["ids"] if key in allowed)
            )
            # Fuse lexical and vector lists before truncation, retaining exact hits.
            fused = Counter()
            for order in (hybrid_ids, [row["knowledge_id"] for row in baseline]):
                for rank, key in enumerate(order):
                    fused[key] += 1 / (10 + rank)
            retrieved = [
                allowed[key]
                for key, _ in sorted(
                    fused.items(), key=lambda item: (-item[1], item[0])
                )
            ][: opts["prefetch_limit"]]
            effective = "qdrant"
            index_binding = result["binding"]
        except InboundClaimLost:
            raise
        except Exception as exc:  # noqa: BLE001 - provider failure must select an explicit recorded fallback, never implicit success
            fallback = type(exc).__name__
            retrieved = baseline if opts["allow_fallback"] else []
            effective = "sqlite" if opts["allow_fallback"] else "unavailable"
    # Re-read all authority/content after provider calls, before sending metadata.
    if context_binding is not None:
        _input_facts(conn, query=query, observed_scope=None, context_binding=context_binding, chat_id=chat_id)
    current, _ = eligible_rows(
        conn,
        candidate_rows(conn, [row['knowledge_id'] for row in retrieved]) if generation is not None else corpus_rows(conn),
        requester_id=requester_id,
        chat_id=chat_id,
        scope=scope,
        verified_profile=verified_profile,
        facts=facts,
    )
    current_by_id = {row["knowledge_id"]: row for row in current}
    retrieved = [
        row
        for row in retrieved
        if row["knowledge_id"] in current_by_id
        and entry_fingerprint(row)
        == entry_fingerprint(current_by_id[row["knowledge_id"]])
    ]
    if effective == "qdrant" and retrieved:
        try:
            ranked = hybrid.rerank(query=query, rows=retrieved)
            allowed = {row["knowledge_id"]: row for row in retrieved}
            if set(ranked) != set(allowed) or len(ranked) != len(allowed):
                raise RetrievalError("invalid reranker IDs")
            retrieved = [allowed[key] for key in ranked]
        except InboundClaimLost:
            raise
        except Exception as exc:  # noqa: BLE001 - arbitrary reranker failures must retain the fail-closed fallback contract
            fallback = type(exc).__name__
            effective = "sqlite" if opts["allow_fallback"] else "unavailable"
            retrieved = (
                lexical_recall(
                    current,
                    query,
                    opts["candidate_limit"],
                    fts_ranks=_fts_ranks(conn, current, query),
                )
                if opts["allow_fallback"]
                else []
            )
    # Reranking may be slow too. Revalidate immediately before selector disclosure.
    if context_binding is not None:
        _input_facts(conn, query=query, observed_scope=None, context_binding=context_binding, chat_id=chat_id)
    fresh, _ = eligible_rows(
        conn,
        candidate_rows(conn, [row['knowledge_id'] for row in retrieved]),
        requester_id=requester_id,
        chat_id=chat_id,
        scope=scope,
        verified_profile=verified_profile,
        facts=facts,
    )
    fresh_by_id = {row["knowledge_id"]: row for row in fresh}
    retrieved = [
        row
        for row in retrieved
        if row["knowledge_id"] in fresh_by_id
        and entry_fingerprint(row)
        == entry_fingerprint(fresh_by_id[row["knowledge_id"]])
    ][: opts["candidate_limit"]]
    catalog = [entry_metadata(row) for row in retrieved]
    binding = {
        "version": 1,
        "tokenizer": TOKENIZER_VERSION,
        "selection_contract": SELECTION_CONTRACT,
        "scope_policy": SCOPE_POLICY,
        "context_scope_policy": CONTEXT_FACT_POLICY,
        "options": opts,
        "minimum_confidence": minimum_confidence,
        "effective_backend": effective,
        "corpus_digest": full_corpus_digest,
        "index": index_binding if effective == "qdrant" else None,
        "selection": _selector_identity(selector),
    }
    if generation is not None:
        binding['corpus_generation'] = generation['revision']
        binding['sources_digest'] = generation['sources_digest']
    output = {
        "retrieved_knowledge_ids": [row["knowledge_id"] for row in retrieved],
        "candidates": catalog,
        "retrieved_entries": [_public_entry(row) for row in retrieved],
        "selected_knowledge_id": None,
        "selected_entry": None,
        "abstention_reason": recall_issue or "no_match",
        "scope": scope,
        "scope_facts": facts,
        "runtime_binding": binding,
        "trace": {
            "effective_backend": effective,
            "fallback": fallback,
            "filter_counts": dict(reasons),
        },
    }
    selected, confidence = None, 1.0
    if retrieved and selector is None:
        # Neither lexical overlap nor nearest-neighbor rank proves answerability.
        # Retain candidates for research on every backend, including fallback.
        output['abstention_reason'] = 'semantic_selection_required'
    elif retrieved:
        # The provider handles recoverable model failures with None. Unexpected
        # exceptions (including lost ownership) propagate to the worker retry.
        from .semantic import observed_inference

        observation_token = observed_inference.set(None)
        try:
            selection = selector(query, catalog)
            observation = observed_inference.get()
        finally:
            observed_inference.reset(observation_token)
        if isinstance(observation, dict) and observation.get("result_digest") == digest(selection):
            output["trace"]["selection_observation"] = observation
            descriptor_fields = ("model", "provider", "manifest_digest")
            if (observation.get("verification") == "bridge_sdk_observation"
                    and all(isinstance(observation.get(key), str) and observation[key]
                            for key in descriptor_fields)):
                binding["selection"] = {key: observation[key] for key in descriptor_fields}
                binding["selection"].update(verification="bridge_sdk_observation", protocol=2)
        if isinstance(selection, dict) and set(selection) == {
            "knowledge_id",
            "confidence",
        }:
            confidence = selection["confidence"]
            if (
                type(confidence) in {int, float}
                and minimum_confidence <= confidence <= 1
            ):
                selected = next(
                    (
                        row
                        for row in retrieved
                        if row["knowledge_id"] == selection["knowledge_id"]
                    ),
                    None,
                )
    if selected is not None:
        if context_binding is not None:
            _input_facts(conn, query=query, observed_scope=None, context_binding=context_binding, chat_id=chat_id)
        fresh, _ = eligible_rows(
            conn,
            candidate_rows(conn, [selected['knowledge_id']]),
            requester_id=requester_id,
            chat_id=chat_id,
            scope=scope,
            verified_profile=verified_profile,
            facts=facts,
        )
        row = next(
            (
                row
                for row in fresh
                if row["knowledge_id"] == selected["knowledge_id"]
                and entry_fingerprint(row) == entry_fingerprint(selected)
            ),
            None,
        )
        if row is None:
            output["abstention_reason"] = "stale_evidence"
        elif missing := _scope_state(row, scope, facts):
            output["abstention_reason"] = missing
        else:
            payload = _json(row.get("revision_payload"), {})
            claims = [
                claim["id"]
                for claim in payload.get("claims", [])
                if isinstance(claim, dict) and isinstance(claim.get("id"), str)
            ]
            output.update(
                selected_knowledge_id=row["knowledge_id"],
                selected_entry={
                    **_public_entry(row),
                    "semantic_match_confidence": float(confidence),
                    "knowledge_runtime_binding": binding,
                    "knowledge_query_digest": digest(query),
                    "knowledge_requester_id": requester_id,
                    "knowledge_chat_id": chat_id,
                    "knowledge_profile_digest": digest(verified_profile),
                    "knowledge_observed_scope": scope,
                    "knowledge_scope_facts": facts,
                    "knowledge_claim_ids": claims,
                    "knowledge_entry_fingerprint": entry_fingerprint(row),
                    **({"knowledge_selection_observation": output["trace"]["selection_observation"]}
                       if "selection_observation" in output["trace"] else {}),
                    **({'knowledge_context_binding': dict(context_binding)} if context_binding is not None else {}),
                },
                abstention_reason=None,
            )
    if context_binding is not None:
        _input_facts(conn, query=query, observed_scope=None, context_binding=context_binding, chat_id=chat_id)
    if generation is not None:
        latest = corpus_state(conn)
        if not latest['current'] or latest['revision'] != generation['revision']:
            output.update(selected_knowledge_id=None, selected_entry=None,
                          abstention_reason='corpus_not_current')
    output["trace"]["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
    return output
