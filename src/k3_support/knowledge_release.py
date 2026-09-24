"""Owner-signed knowledge release verification; never a signing authority.

The runtime contains public-key verification only. Production trust must be
provisioned outside the agent's writable filesystem boundary. SQLite review
strings and locally recomputed hashes cannot authorize an automatic reply.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from importlib import metadata, resources
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .ids import canonical_json, digest
from .knowledge_eval import evaluate_items

RELEASE_VERSION = 1
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_SHA = re.compile(r"[a-f0-9]{64}")


class KnowledgeReleaseError(ValueError):
    pass


def _time(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError("timezone missing")
        return result.astimezone(UTC)
    except (TypeError, ValueError) as exc:
        raise KnowledgeReleaseError("release time must include a timezone") from exc


def _json_file(path: Path, *, protected: bool = False) -> dict[str, Any]:
    """Bounded no-follow file read; trust paths also reject writable ancestors."""
    if not path.is_absolute() or ".." in path.parts:
        raise KnowledgeReleaseError("release path must be absolute without parent traversal")
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise KnowledgeReleaseError("release paths must not contain symlinks")
        if protected and (info.st_uid != 0 or info.st_mode & 0o022):
            raise KnowledgeReleaseError("trust policy must be root-owned outside agent-writable paths")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES:
            raise KnowledgeReleaseError("release input must be a bounded regular file")
        if protected and (info.st_uid != 0 or info.st_mode & 0o022):
            raise KnowledgeReleaseError("trust policy permissions changed")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_ARTIFACT_BYTES + 1)
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise KnowledgeReleaseError("release input exceeds size limit")
        value = json.loads(raw)
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise KnowledgeReleaseError("release input must be a JSON object")
    return value


def load_trust_policy(path: Path) -> dict[str, Any]:
    policy = _json_file(path, protected=True)
    if set(policy) != {"schema_version", "instance_id", "keys", "revoked_release_ids", "max_validity_seconds"}:
        raise KnowledgeReleaseError("trust policy fields do not match schema")
    if policy["schema_version"] != 1 or not isinstance(policy["instance_id"], str) or not policy["instance_id"]:
        raise KnowledgeReleaseError("trust policy instance/version is invalid")
    keys = policy["keys"]
    if not isinstance(keys, dict) or not keys:
        raise KnowledgeReleaseError("trust policy has no independently pinned keys")
    for key_id, encoded in keys.items():
        if not isinstance(key_id, str) or not key_id or not isinstance(encoded, str):
            raise KnowledgeReleaseError("invalid trust key")
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(encoded, validate=True))
        except (ValueError, TypeError) as exc:
            raise KnowledgeReleaseError("trust key must be raw Ed25519 public bytes in base64") from exc
    revoked = policy["revoked_release_ids"]
    if not isinstance(revoked, list) or any(not isinstance(item, str) for item in revoked):
        raise KnowledgeReleaseError("invalid release revocation list")
    duration = policy["max_validity_seconds"]
    if type(duration) is not int or not 1 <= duration <= 31 * 86400:
        raise KnowledgeReleaseError("release maximum validity must be 1 second to 31 days")
    return {**policy, "accepted_evidence_class": "human_reviewed"}


def code_manifest() -> dict[str, Any]:
    """Hash shipped code/resources, independent of checkout/venv location."""
    files: dict[str, str] = {}

    def walk(node, prefix=""):
        for child in sorted(node.iterdir(), key=lambda item: item.name):
            if child.name == "__pycache__":
                continue
            relative = prefix + child.name
            if child.is_dir():
                walk(child, relative + "/")
            elif child.name.endswith((".py", ".sql", ".json", ".md", ".yaml", ".toml")):
                files[relative] = hashlib.sha256(child.read_bytes()).hexdigest()

    walk(resources.files("k3_support"))
    dependencies = {}
    for package in ("cryptography", "jsonschema", "PyYAML", "qdrant-client"):
        try:
            dependencies[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            dependencies[package] = None
    return {"files": files, "dependencies": dependencies}


def knowledge_snapshot(conn) -> dict[str, Any]:
    """Bind actual content/ACL/source/claim state, not usage counters or polling."""
    from .knowledge_runtime import corpus_fingerprint, corpus_rows, entry_fingerprint

    rows = corpus_rows(conn)
    source_rows = [dict(row) for row in conn.execute(
        """SELECT ks.knowledge_id,ks.source_type,ks.stable_external_id,ks.claim,ks.visibility,
                  sr.title,sr.url,sr.acl_json,sr.source_version,sr.content_digest
             FROM knowledge_sources ks LEFT JOIN source_registry sr
               ON sr.source_type=ks.source_type AND sr.stable_external_id=ks.stable_external_id
             JOIN knowledge_entries ke ON ke.knowledge_id=ks.knowledge_id
            WHERE ke.status='approved'
            ORDER BY ks.knowledge_id,ks.source_type,ks.stable_external_id,ks.claim"""
    )]
    entries = {}
    legacy = []
    for row in rows:
        if not row.get("professional_revision_id"):
            legacy.append(row["knowledge_id"])
            continue
        payload = json.loads(row["revision_payload"])
        if row["revision_state"] != "published" or not payload.get("publication", {}).get("automatic_reply"):
            continue
        entries[row["knowledge_id"]] = {
            "stable_id": payload["id"], "revision_digest": row["revision_digest"],
            "fingerprint": entry_fingerprint(row),
            "claim_ids": sorted(item["id"] for item in payload["claims"]),
            "review_due_at": row["review_due_at"],
        }
    return {"corpus_digest": corpus_fingerprint(rows), "sources_digest": digest(source_rows),
            "entries": entries, "legacy_search_only_ids": sorted(legacy)}


def policy_binding(config) -> dict[str, Any]:
    # Runtime mode and counters may change; reviewed routing/disclosure policy may not.
    return {"routing": config.raw["routing"], "scope": config.raw["scope"],
            "minimum_confidence": config.raw["policy"]["auto_reply_confidence"],
            "retrieval": config.raw["knowledge_retrieval"]}


def current_binding(conn, config) -> dict[str, Any]:
    snapshot = knowledge_snapshot(conn)
    return {"code_digest": digest(code_manifest()), "knowledge_digest": digest(snapshot),
            "policy_digest": digest(policy_binding(config))}


def _runtime_ready(binding: Any) -> bool:
    if not isinstance(binding, dict):
        return False
    selection = binding.get("selection", {})
    # Require the result-bound SDK observation emitted by the shared query path.
    # This descriptor must still match the independently signed release below;
    # it is not cryptographic attestation of a remote provider's implementation.
    if (not isinstance(selection, dict)
            or set(selection) != {'verification', 'protocol', 'model', 'provider', 'manifest_digest'}
            or selection.get('verification') != 'bridge_sdk_observation'
            or type(selection.get('protocol')) is not int or selection['protocol'] != 2
            or any(not isinstance(selection.get(key), str) or not selection[key].strip()
                   for key in ('model', 'provider'))
            or not isinstance(selection.get('manifest_digest'), str)
            or not _SHA.fullmatch(selection['manifest_digest'])):
        return False
    if binding.get("effective_backend") == "sqlite":
        return binding.get("index") is None
    # BGE/index verification remains a separate gate, not implied by selection.
    return False


def verify_release(conn, config, *, runtime_binding: dict | None = None,
                   knowledge_ids: list[str] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Read current trust every time; an invalid release never raises readiness."""
    setting = config.raw.get("knowledge_release", {})
    try:
        if not setting.get("trust_policy_path") or not setting.get("artifact_path"):
            raise KnowledgeReleaseError("knowledge_release_unconfigured")
        policy = load_trust_policy(Path(setting["trust_policy_path"]))
        envelope = _json_file(Path(setting["artifact_path"]))
        if set(envelope) != {"payload", "signature"} or not isinstance(envelope["payload"], dict):
            raise KnowledgeReleaseError("release envelope fields are invalid")
        payload = envelope["payload"]
        required = {"schema_version", "release_id", "instance_id", "key_id", "issued_at", "expires_at",
                    "evidence_class", "binding", "entries", "runtime_bindings", "evaluation"}
        if set(payload) != required or payload["schema_version"] != RELEASE_VERSION:
            raise KnowledgeReleaseError("release payload fields are invalid")
        key = policy["keys"].get(payload["key_id"])
        if key is None:
            raise KnowledgeReleaseError("release key is not independently pinned")
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key, validate=True)).verify(
            base64.b64decode(envelope["signature"], validate=True), canonical_json(payload).encode(),
        )
        if payload["instance_id"] != policy["instance_id"]:
            raise KnowledgeReleaseError("release belongs to another instance")
        if payload["release_id"] in policy["revoked_release_ids"]:
            raise KnowledgeReleaseError("release_revoked")
        if payload["evidence_class"] != policy["accepted_evidence_class"]:
            raise KnowledgeReleaseError("synthetic_or_unreviewed_release_not_authorized")
        observed = now or datetime.now(UTC)
        if observed.tzinfo is None:
            raise KnowledgeReleaseError("verification time must include timezone")
        issued, expires = _time(payload["issued_at"]), _time(payload["expires_at"])
        if not issued <= observed < expires or (expires - issued).total_seconds() > policy["max_validity_seconds"]:
            raise KnowledgeReleaseError("release_expired_or_not_yet_valid")
        if payload["binding"] != current_binding(conn, config):
            raise KnowledgeReleaseError("release_code_knowledge_or_policy_changed")
        snapshot = knowledge_snapshot(conn)
        if not payload["entries"] or payload["entries"] != snapshot["entries"]:
            raise KnowledgeReleaseError("release_eligible_entries_changed_or_empty")
        if any(_time(entry["review_due_at"]) <= observed for entry in payload["entries"].values()):
            raise KnowledgeReleaseError("release_article_review_expired")
        evaluation = payload["evaluation"]
        if not isinstance(evaluation, dict) or evaluation.get("origin") != "actual_query_runtime":
            raise KnowledgeReleaseError("release_requires_actual_runtime_predictions")
        for key in ("gold_manifest_digest", "gold_digest", "review_digest", "request_inputs_digest"):
            if not isinstance(evaluation.get(key), str) or not _SHA.fullmatch(evaluation[key]):
                raise KnowledgeReleaseError("release_evaluation_provenance_missing")
        predictions, gold = evaluation["predictions"], evaluation["gold"]
        from .knowledge_gold_review import verify_embedded_gold_bundle

        reviewed = verify_embedded_gold_bundle(
            evaluation["gold_manifest"], evaluation["reviews"], gold, evaluation["gold_manifest_digest"],
        )
        if not reviewed["complete"] or any(reviewed[key] != evaluation[key] for key in ("gold_digest", "review_digest")):
            raise KnowledgeReleaseError("release_gold_evidence_changed_or_partial")
        inputs = evaluation["request_inputs"]
        if digest(inputs) != evaluation["request_inputs_digest"]:
            raise KnowledgeReleaseError("release_request_inputs_changed")
        if not isinstance(inputs, list) or len(inputs) != len(gold) or any(
            not isinstance(item, dict) or set(item) != {"id", "query", "requester_id", "chat_id", "observed_scope", "verified_profile"}
            or item["id"] != case["id"] or item["query"] != case["query"]
            for item, case in zip(inputs, gold, strict=True)
        ):
            raise KnowledgeReleaseError("release_request_inputs_do_not_match_reviewed_questions")
        if digest(predictions) != evaluation["predictions_digest"]:
            raise KnowledgeReleaseError("release_predictions_changed")
        report = evaluate_items(gold, predictions)
        if report != evaluation["report"] or not report["ready_for_automatic_reply"]:
            raise KnowledgeReleaseError("release_quality_gate_failed")
        covered = {item["selected_knowledge_id"] for item in predictions if item["answered"]}
        if not {entry["stable_id"] for entry in payload["entries"].values()} <= covered:
            raise KnowledgeReleaseError("release_has_unevaluated_articles")
        bindings = payload["runtime_bindings"]
        if not isinstance(bindings, list) or not bindings or not all(_runtime_ready(item) for item in bindings):
            raise KnowledgeReleaseError("release_model_or_backend_unverified")
        if runtime_binding is not None and runtime_binding not in bindings:
            raise KnowledgeReleaseError("release_runtime_or_fallback_changed")
        if knowledge_ids is not None and (not knowledge_ids or not set(knowledge_ids) <= set(payload["entries"])):
            raise KnowledgeReleaseError("knowledge_not_covered_by_release")
        runtime_verified = runtime_binding is not None
        return {"ready": runtime_verified, "artifact_verified": True,
                "reason": None if runtime_verified else "current_runtime_not_verified",
                "release_id": payload["release_id"],
                "release_digest": digest(payload), "expires_at": payload["expires_at"],
                "entries": payload["entries"], "runtime_bindings": bindings,
                "evidence_class": payload["evidence_class"], "sample_size": report["metrics"]["sample_size"]}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, InvalidSignature) as exc:
        reason = "release_signature_invalid" if isinstance(exc, InvalidSignature) else str(exc)
        return {"ready": False, "reason": reason or type(exc).__name__, "release_id": None}


def bind_knowledge_reply(conn, config, *, source_event_pk: str, knowledge_ids: list[str], text: str) -> dict:
    """Bind the persisted actual query result; never invent provenance for old rows."""
    row = conn.execute(
        "SELECT knowledge_id,knowledge_runtime_json FROM route_decisions WHERE event_pk=? ORDER BY created_at DESC LIMIT 1",
        (source_event_pk,),
    ).fetchone()
    provenance = json.loads(row["knowledge_runtime_json"]) if row else {}
    runtime = provenance.get("knowledge_runtime_binding")
    verified = verify_release(conn, config, runtime_binding=runtime, knowledge_ids=knowledge_ids)
    return {"source_event_pk": source_event_pk, "knowledge_ids": sorted(set(knowledge_ids)),
            "provenance": provenance, "text_digest": digest(text),
            "release_digest": verified.get("release_digest") if verified["ready"] else None,
            "release_block_reason": verified.get("reason")}


def _verified_case_reply(conn, row: dict, payload: dict) -> bool:
    """Reconstruct the reviewed decision; a caller-supplied label is no proof.

    These persisted records are consistency checks, not an OS privilege boundary.
    Readiness separately requires an independently isolated execution authority.
    """
    from .message_format import format_feishu_ai_message

    ids = payload.get("evidence_ids")
    if (not isinstance(ids, list) or not ids or any(not isinstance(key, str) for key in ids)
            or len(ids) != len(set(ids)) or not row.get("case_id")):
        return False
    reviews = conn.execute(
        """SELECT cr.*,ce.detail_json,j.lifecycle_round,c.lifecycle_round AS current_round
             FROM codex_reviews cr JOIN jobs j ON j.job_id=cr.job_id
             JOIN cases c ON c.case_id=cr.case_id
             JOIN case_events ce ON ce.case_id=cr.case_id
               AND ce.idempotency_key='decision:' || cr.decision_id
            WHERE cr.case_id=? AND cr.status='decision_applied'
              AND ce.event_type='decision_applied' AND j.state='succeeded'""",
        (row["case_id"],),
    ).fetchall()
    for review in reviews:
        detail = json.loads(review["detail_json"])
        decision = detail.get("decision", {})
        if (row["outbox_id"] not in detail.get("outbox_ids", [])
                or review["lifecycle_round"] != review["current_round"]
                or decision != json.loads(review["hermes_output_json"] or "null")
                or decision.get("decision_id") != review["decision_id"]
                or decision.get("case_id") != row["case_id"] or decision.get("intent") != "reply"
                or decision.get("evidence_ids") != ids
                or not set(ids) <= set(json.loads(review["evidence_ids_json"]))
                or format_feishu_ai_message(decision.get("reply_draft")) != payload.get("text")):
            continue
        action = {"type": "feishu_reply", "source_event_pk": row.get("source_event_pk"),
                  "source_message_id": row["destination"]}
        if (decision.get("proposed_actions") != [action]
                or row.get("idempotency_key") != f"decision:{review['decision_id']}:action:0"):
            continue
        evidence = conn.execute(
            f"""SELECT e.*,cs.source_type,cs.stable_external_id,cs.requester_access
                  FROM evidence e JOIN case_sources cs ON cs.source_id=e.source_id AND cs.case_id=e.case_id
                 WHERE e.case_id=? AND e.evidence_id IN ({','.join('?' for _ in ids)})""",
            (row["case_id"], *ids),
        ).fetchall()
        if len(evidence) != len(ids):
            continue
        checks = json.loads(review["independent_checks_json"])
        valid = True
        for item in evidence:
            check = json.loads(item["result"])
            if item["source_type"] == "codex_remote_git":
                checked = (item["requester_access"] == "allowed" and isinstance(check, dict)
                           and check.get("kind") == "git" and item["evidence_layer"] == "static"
                           and check.get("verified") is True and check in checks
                           and item["stable_external_id"] == f"{review['job_id']}:{check.get('repo')}"
                           and item["artifact_hash"] == (check.get("output_sha256") or check.get("head_commit")))
            else:
                from .review_evidence import verified_non_git_evidence

                checked = verified_non_git_evidence(conn, review=review, item=item, check=check, checks=checks)
            if not checked:
                valid = False
                break
        if valid:
            return True
    return False


def verify_knowledge_reply(conn, config, *, row: dict, payload: dict) -> dict:
    """Final Outbox gate, including legacy and dynamically discovered document links."""
    if row["channel"] != "feishu_im" or row["action_type"] != "reply":
        return {"ready": True, "reason": None}
    from .conversation_context import context_snapshot, validate_context_binding

    current_context = None
    source = conn.execute('SELECT source FROM inbound_events WHERE event_pk=?', (row.get('source_event_pk'),)).fetchone()
    if source is not None and source['source'] in {'feishu_bot_im', 'feishu_user_poll'}:
        valid, reason = validate_context_binding(conn, row, require_ai=True)
        if not valid:
            return {'ready': False, 'reason': reason}
        current_context = context_snapshot(conn, row['context_id'])
        if (current_context['case_id'] != row.get('case_id') or current_context['lifecycle_round'] != row.get('lifecycle_round')
                or current_context['focus_event_pk'] != row.get('source_event_pk')):
            return {'ready': False, 'reason': 'knowledge_reply_context_target_changed'}
    # Case-specific reviewed coding evidence has a separate review/authorization
    # path. New decisions retain evidence IDs so mixed knowledge cannot masquerade
    # as a purely Case-specific answer by changing only reply_basis.
    evidence_ids = payload.get("evidence_ids") or []
    knowledge_evidence = []
    if isinstance(evidence_ids, list) and evidence_ids:
        knowledge_evidence = conn.execute(
            f"""SELECT cs.stable_external_id FROM evidence e JOIN case_sources cs ON cs.source_id=e.source_id
                 WHERE e.case_id=? AND e.evidence_id IN ({','.join('?' for _ in evidence_ids)})
                   AND cs.source_type='approved_knowledge'""", (row.get("case_id"), *evidence_ids),
        ).fetchall()
    if payload.get("reply_basis") == "verified_evidence" and not knowledge_evidence and not payload.get("knowledge_release"):
        try:
            if _verified_case_reply(conn, row, payload):
                return {"ready": True, "reason": None}
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        return {"ready": False, "reason": "case_reply_has_no_bound_independent_review"}
    binding = payload.get("knowledge_release")
    if not isinstance(binding, dict) or not binding.get("release_digest"):
        return {"ready": False, "reason": "knowledge_reply_has_no_evaluated_release"}
    try:
        if binding["text_digest"] != digest(payload["text"]) or binding["source_event_pk"] != row.get("source_event_pk"):
            raise KnowledgeReleaseError("knowledge_reply_binding_changed")
        ids = binding["knowledge_ids"]
        if not isinstance(ids, list) or not ids or any(not isinstance(key, str) for key in ids):
            raise KnowledgeReleaseError("knowledge_reply_ids_missing")
        observed_ids = {item[0] for item in knowledge_evidence}
        if observed_ids and observed_ids != set(ids):
            raise KnowledgeReleaseError("knowledge_reply_evidence_changed")
        route = conn.execute(
            "SELECT knowledge_id,knowledge_runtime_json FROM route_decisions WHERE event_pk=? ORDER BY created_at DESC LIMIT 1",
            (row["source_event_pk"],),
        ).fetchone()
        if route is None or ids != [route["knowledge_id"]]:
            raise KnowledgeReleaseError("knowledge_reply_has_no_actual_query_selection")
        provenance = binding["provenance"]
        if provenance != json.loads(route["knowledge_runtime_json"]) or not provenance:
            raise KnowledgeReleaseError("knowledge_reply_provenance_changed")
        runtime = provenance["knowledge_runtime_binding"]
        verified = verify_release(conn, config, runtime_binding=runtime, knowledge_ids=ids)
        if not verified["ready"]:
            return verified
        if binding["release_digest"] != verified["release_digest"]:
            raise KnowledgeReleaseError("knowledge_reply_release_replaced_requery_required")
        entry = verified["entries"][ids[0]]
        if provenance["knowledge_entry_fingerprint"] != entry["fingerprint"]:
            raise KnowledgeReleaseError("knowledge_reply_article_changed")
        if set(provenance["knowledge_claim_ids"]) != set(entry["claim_ids"]):
            raise KnowledgeReleaseError("knowledge_reply_claims_changed")
        from .knowledge_runtime import (
            event_input_digest,
            event_query,
            load_approved_entry,
        )

        event = conn.execute("SELECT * FROM inbound_events WHERE event_pk=?", (row["source_event_pk"],)).fetchone()
        if event is None:
            raise KnowledgeReleaseError("knowledge_reply_source_missing")
        current_query = current_context['query'] if current_context else event_query(event)
        if current_context is not None and (provenance.get('knowledge_context_binding') != current_context['binding']
                or provenance.get('knowledge_scope_facts') != current_context['facts']
                or provenance.get('knowledge_observed_scope') != current_context['facts']['observed_scope']):
            raise KnowledgeReleaseError('knowledge_reply_context_facts_changed')
        if (provenance.get("knowledge_query_digest") != digest(current_query)
                or provenance.get("knowledge_event_digest") != event_input_digest(event)
                or provenance.get("knowledge_requester_id") != event["sender_id"]
                or provenance.get("knowledge_chat_id") != event["chat_id"]
                or row["destination"] != event["external_id"]
                or payload.get("identity") != event["identity"]):
            raise KnowledgeReleaseError("knowledge_reply_original_request_changed")
        fresh_entries = [load_approved_entry(
            conn, knowledge_id=key, requester_id=event["sender_id"], chat_id=event["chat_id"],
            query=current_query,
            observed_scope=None if current_context else provenance["knowledge_observed_scope"],
            context_binding=current_context['binding'] if current_context else None,
        ) for key in ids]
        if any(entry is None for entry in fresh_entries):
            raise KnowledgeReleaseError("knowledge_reply_acl_or_scope_changed")
        from .knowledge_answer import approved_answer_markdown
        from .message_format import format_feishu_ai_message

        if payload["text"] != format_feishu_ai_message(approved_answer_markdown(conn, fresh_entries[0])):
            raise KnowledgeReleaseError("knowledge_reply_is_not_evaluated_answer")
        return verified
    except (KeyError, TypeError, ValueError) as exc:
        return {"ready": False, "reason": str(exc) or "invalid_knowledge_reply_binding"}
