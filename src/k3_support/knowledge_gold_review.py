from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .ids import canonical_json


class KnowledgeGoldReviewError(ValueError):
    pass


_MAX_INPUT_BYTES = 100 * 1024 * 1024
_MAX_LINE_BYTES = 1_000_000


def _validator(name: str) -> Draft202012Validator:
    path = resources.files("k3_support").joinpath(f"schemas/{name}")
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def _private_regular_file(path: Path, *, label: str) -> bytes:
    path = Path(os.path.abspath(path.expanduser()))
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise KnowledgeGoldReviewError(
            f"{label} must be a readable regular file: {path}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise KnowledgeGoldReviewError(f"{label} must be a regular file: {path}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise KnowledgeGoldReviewError(
                f"{label} must not be group/world accessible"
            )
        if info.st_size > _MAX_INPUT_BYTES:
            raise KnowledgeGoldReviewError(f"{label} exceeds 100 MiB")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(_MAX_INPUT_BYTES + 1)
            if len(payload) > _MAX_INPUT_BYTES:
                raise KnowledgeGoldReviewError(f"{label} exceeds 100 MiB")
            return payload
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _parse_jsonl(raw: bytes, *, label: str, schema_name: str) -> list[dict[str, Any]]:
    validator = _validator(schema_name)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        if len(line) > _MAX_LINE_BYTES:
            raise KnowledgeGoldReviewError(f"{label} line {line_number} exceeds 1 MiB")
        try:
            item = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KnowledgeGoldReviewError(
                f"{label} line {line_number} is invalid JSON"
            ) from exc
        errors = sorted(validator.iter_errors(item), key=lambda error: list(error.path))
        if errors:
            error = errors[0]
            location = ".".join(str(part) for part in error.path) or "item"
            raise KnowledgeGoldReviewError(
                f"{label} line {line_number} {location}: {error.message}"
            )
        item_id = str(item.get("id") or item.get("candidate_id"))
        if item_id in seen:
            raise KnowledgeGoldReviewError(f"duplicate {label} id: {item_id}")
        seen.add(item_id)
        items.append(item)
    if not items:
        raise KnowledgeGoldReviewError(f"{label} is empty")
    return items


def _load_jsonl(
    path: Path, *, label: str, schema_name: str
) -> tuple[list[dict[str, Any]], str]:
    raw = _private_regular_file(path, label=label)
    return (
        _parse_jsonl(raw, label=label, schema_name=schema_name),
        hashlib.sha256(raw).hexdigest(),
    )


def _parse_reviewed_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise KnowledgeGoldReviewError(f"invalid reviewed_at: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise KnowledgeGoldReviewError("reviewed_at must include a timezone")
    return parsed


def _query_digest(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _validate_final_label(candidate_id: str, label: dict[str, Any]) -> None:
    answerable = bool(label["answerable"])
    direct_answer = label["expected_route"] == "direct_answer"
    if answerable != direct_answer:
        raise KnowledgeGoldReviewError(
            f"{candidate_id}: direct_answer and answerable must agree"
        )
    if answerable and not label["allowed_knowledge_ids"]:
        raise KnowledgeGoldReviewError(
            f"{candidate_id}: answerable review needs allowed knowledge"
        )
    if answerable and not label["allowed_claim_ids"]:
        raise KnowledgeGoldReviewError(
            f"{candidate_id}: answerable review needs allowed claims"
        )
    if not answerable and not label["acceptable_abstention_reasons"]:
        raise KnowledgeGoldReviewError(
            f"{candidate_id}: unanswerable review needs an abstention reason"
        )
    overlap = set(label["allowed_knowledge_ids"]) & set(
        label["forbidden_knowledge_ids"]
    )
    if overlap:
        raise KnowledgeGoldReviewError(
            f"{candidate_id}: allowed and forbidden knowledge overlap"
        )
    tags = set(label["tags"])
    if "human_reviewed" not in tags:
        raise KnowledgeGoldReviewError(
            f"{candidate_id}: accepted label needs the human_reviewed tag"
        )
    proposal_only = {"model_suggestion_unreviewed", "requires_human_scope_review", "route_not_adjudicated"}
    if tags & proposal_only:
        raise KnowledgeGoldReviewError(
            f"{candidate_id}: remove proposal-only tags after review"
        )


def _encode_jsonl(items: list[dict[str, Any]]) -> bytes:
    return "".join(canonical_json(item) + "\n" for item in items).encode("utf-8")


def _private_write(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_private_create(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".partial-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise KnowledgeGoldReviewError(
                f"refusing to overwrite review file: {path}"
            ) from exc
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _existing_bundle_matches(output_dir: Path, expected: dict[str, bytes]) -> bool:
    if output_dir.is_symlink() or not output_dir.is_dir():
        return False
    if stat.S_IMODE(output_dir.stat().st_mode) & 0o077:
        return False
    for name, payload in expected.items():
        path = output_dir / name
        if path.is_symlink() or not path.is_file():
            return False
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            return False
        if path.read_bytes() != payload:
            return False
    return {item.name for item in output_dir.iterdir()} == set(expected)


def _verify_gold_bundle(
    output_dir: Path, *, expected_manifest_digest: str | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Verify a promoted gold bundle without trusting filenames or metadata alone."""
    output = Path(os.path.abspath(output_dir.expanduser()))
    if output.is_symlink() or not output.is_dir():
        raise KnowledgeGoldReviewError(f"gold bundle must be a directory: {output}")
    if stat.S_IMODE(output.stat().st_mode) & 0o077:
        raise KnowledgeGoldReviewError("gold bundle must not be group/world accessible")
    expected_names = {"gold.jsonl", "reviews.jsonl", "manifest.json"}
    actual_names = {item.name for item in output.iterdir()}
    if actual_names != expected_names:
        raise KnowledgeGoldReviewError(
            f"gold bundle files differ; expected={sorted(expected_names)}, actual={sorted(actual_names)}"
        )

    gold_payload = _private_regular_file(output / "gold.jsonl", label="gold file")
    reviews_payload = _private_regular_file(
        output / "reviews.jsonl", label="bundled review file"
    )
    manifest_payload = _private_regular_file(
        output / "manifest.json", label="gold manifest"
    )
    verification, gold = _verify_gold_payloads(
        manifest_payload=manifest_payload,
        reviews_payload=reviews_payload,
        gold_payload=gold_payload,
        expected_manifest_digest=expected_manifest_digest,
    )
    return {"output": str(output), **verification}, gold


def _verify_gold_payloads(
    *, manifest_payload: bytes, reviews_payload: bytes, gold_payload: bytes,
    expected_manifest_digest: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One strict verification core for private files and signed embedded data."""
    if any(len(raw) > _MAX_INPUT_BYTES for raw in (manifest_payload, reviews_payload, gold_payload)):
        raise KnowledgeGoldReviewError("gold bundle input exceeds 100 MiB")
    try:
        manifest = json.loads(manifest_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KnowledgeGoldReviewError("gold manifest is invalid JSON") from exc
    errors = sorted(
        _validator("knowledge-evaluation-gold-manifest-v1.json").iter_errors(manifest),
        key=lambda error: list(error.path),
    )
    if errors:
        raise KnowledgeGoldReviewError(f"gold manifest: {errors[0].message}")
    if manifest_payload != (canonical_json(manifest) + "\n").encode("utf-8"):
        raise KnowledgeGoldReviewError("gold manifest is not canonical")
    manifest_digest = hashlib.sha256(manifest_payload).hexdigest()
    if (
        expected_manifest_digest is not None
        and manifest_digest != expected_manifest_digest
    ):
        raise KnowledgeGoldReviewError("gold manifest digest does not match approval")
    if hashlib.sha256(gold_payload).hexdigest() != manifest["gold_digest"]:
        raise KnowledgeGoldReviewError("gold digest does not match manifest")
    if hashlib.sha256(reviews_payload).hexdigest() != manifest["review_digest"]:
        raise KnowledgeGoldReviewError("review digest does not match manifest")

    reviews = _parse_jsonl(
        reviews_payload,
        label="bundled review file",
        schema_name="knowledge-evaluation-review-v1.json",
    )
    if reviews_payload != _encode_jsonl(
        sorted(reviews, key=lambda item: str(item["candidate_id"]))
    ):
        raise KnowledgeGoldReviewError("bundled reviews are not canonical")
    if any(review["candidate_digest"] != manifest["candidate_digest"] for review in reviews):
        raise KnowledgeGoldReviewError("review candidate batch digest does not match manifest")
    from .knowledge_eval import validate_gold_cases

    gold = validate_gold_cases(
        _parse_jsonl(
            gold_payload,
            label="gold file",
            schema_name="knowledge-evaluation-case-v1.json",
        )
    )
    if gold_payload != _encode_jsonl(sorted(gold, key=lambda item: str(item["id"]))):
        raise KnowledgeGoldReviewError("gold cases are not canonical")

    decided = [item for item in reviews if item["decision"] != "pending"]
    accepted = [item for item in decided if item["decision"] == "accepted"]
    rejected = [item for item in decided if item["decision"] == "rejected"]
    gold_ids = [str(item["id"]) for item in gold]
    accepted_ids = [str(item["candidate_id"]) for item in accepted]
    if gold_ids != accepted_ids:
        raise KnowledgeGoldReviewError("accepted review IDs do not match gold cases")
    counts = {
        "reviewed_count": len(decided),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
    }
    if len(reviews) > manifest["candidate_count"]:
        raise KnowledgeGoldReviewError("review rows exceed manifest candidate count")
    for key, value in counts.items():
        if manifest[key] != value:
            raise KnowledgeGoldReviewError(f"manifest {key} does not match contents")
    if manifest["candidate_count"] - len(decided) != manifest["unreviewed_count"]:
        raise KnowledgeGoldReviewError("manifest unreviewed_count does not balance")
    if manifest["complete"] != (manifest["unreviewed_count"] == 0):
        raise KnowledgeGoldReviewError("manifest complete flag does not balance")

    reviewers = sorted({str(item["reviewed_by"]) for item in decided})
    reviewed_times = [_parse_reviewed_at(str(item["reviewed_at"])) for item in decided]
    if manifest["reviewers"] != reviewers:
        raise KnowledgeGoldReviewError("manifest reviewers do not match contents")
    if manifest["reviewed_at_min"] != min(reviewed_times).isoformat():
        raise KnowledgeGoldReviewError(
            "manifest reviewed_at_min does not match contents"
        )
    if manifest["reviewed_at_max"] != max(reviewed_times).isoformat():
        raise KnowledgeGoldReviewError(
            "manifest reviewed_at_max does not match contents"
        )
    manifest_case_ids = [str(item["id"]) for item in manifest["cases"]]
    if manifest_case_ids != gold_ids:
        raise KnowledgeGoldReviewError("manifest cases do not match gold cases")
    review_by_id = {str(item["candidate_id"]): item for item in accepted}
    gold_by_id = {str(item["id"]): item for item in gold}
    for case in manifest["cases"]:
        case_id = str(case["id"])
        review = review_by_id[case_id]
        if review["query_digest"] != _query_digest(gold_by_id[case_id]["query"]):
            raise KnowledgeGoldReviewError(f"review query digest does not match gold case for {case_id}")
        _validate_final_label(case_id, review["label"])
        if (
            case["reviewed_by"] != review["reviewed_by"]
            or case["reviewed_at"] != review["reviewed_at"]
        ):
            raise KnowledgeGoldReviewError(
                f"manifest review identity does not match for {case['id']}"
            )
        final_label = {
            key: value
            for key, value in gold_by_id[case_id].items()
            if key not in {"id", "query"}
        }
        if review["label"] != final_label:
            raise KnowledgeGoldReviewError(
                f"review label does not match gold case for {case_id}"
            )
    return (
        {
            "status": "verified",
            "manifest_digest": manifest_digest,
            **{key: manifest[key] for key in manifest if key != "cases"},
        },
        gold,
    )


def verify_embedded_gold_bundle(
    manifest: dict[str, Any], reviews: list[dict[str, Any]], gold: list[dict[str, Any]],
    expected_manifest_digest: str,
) -> dict[str, Any]:
    """Verify complete embedded evidence without writing a temporary bundle.

    The caller still decides whether a complete review is required. A verified
    partial bundle remains explicitly incomplete, exactly like the file API.
    Array order is preserved, not silently repaired to make a digest pass.
    This checks evidence consistency; it is not an independent signing authority.
    """
    if (
        not isinstance(manifest, dict)
        or not isinstance(reviews, list) or any(not isinstance(item, dict) for item in reviews)
        or not isinstance(gold, list) or any(not isinstance(item, dict) for item in gold)
    ):
        raise KnowledgeGoldReviewError("embedded manifest/reviews/gold have invalid container types")
    if (
        not isinstance(expected_manifest_digest, str) or len(expected_manifest_digest) != 64
        or any(char not in "0123456789abcdef" for char in expected_manifest_digest)
    ):
        raise KnowledgeGoldReviewError("embedded Gold requires an exact approved manifest digest")
    try:
        manifest_payload = (canonical_json(manifest) + "\n").encode("utf-8")
        reviews_payload = _encode_jsonl(reviews)
        gold_payload = _encode_jsonl(gold)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise KnowledgeGoldReviewError("embedded Gold cannot be canonically encoded") from exc
    verification, _ = _verify_gold_payloads(
        manifest_payload=manifest_payload, reviews_payload=reviews_payload,
        gold_payload=gold_payload, expected_manifest_digest=expected_manifest_digest,
    )
    return verification


def verify_gold_bundle(
    output_dir: Path, *, expected_manifest_digest: str | None = None
) -> dict[str, Any]:
    verification, _ = _verify_gold_bundle(
        output_dir, expected_manifest_digest=expected_manifest_digest
    )
    return verification


def evaluate_gold_bundle(
    *,
    bundle_dir: Path,
    predictions_path: Path,
    approved_manifest_digest: str,
    baseline_predictions_path: Path | None = None,
    candidates_path: Path | None = None,
    split_plan_path: Path | None = None,
) -> dict[str, Any]:
    """Evaluate only after the complete private review bundle verifies."""
    verification, gold = _verify_gold_bundle(
        bundle_dir, expected_manifest_digest=approved_manifest_digest
    )
    if not verification["complete"]:
        raise KnowledgeGoldReviewError(
            "partial gold bundle cannot produce trusted release evidence"
        )
    split_report = None
    if (candidates_path is None) != (split_plan_path is None):
        raise KnowledgeGoldReviewError('candidates and split plan must be supplied together')
    if candidates_path is not None:
        from .evaluation_groups import parse_split_plan, select_acceptance_gold

        candidates, candidate_digest = _load_jsonl(candidates_path, label='candidate file',
            schema_name='knowledge-evaluation-candidate-v1.json')
        if candidate_digest != verification['candidate_digest']:
            raise KnowledgeGoldReviewError('split candidates differ from reviewed bundle')
        plan = parse_split_plan(_private_regular_file(split_plan_path, label='split plan'))
        gold, split_report = select_acceptance_gold(candidates, plan,
            candidate_digest=candidate_digest, gold=gold)
    predictions, prediction_digest = _load_jsonl(
        predictions_path,
        label="prediction file",
        schema_name="knowledge-evaluation-prediction-v1.json",
    )
    from .knowledge_eval import evaluate_items

    result = evaluate_items(gold, predictions)
    report = {
        "trusted_gold_bundle": True,
        "gold_digest": verification["gold_digest"],
        "review_digest": verification["review_digest"],
        "candidate_digest": verification["candidate_digest"],
        "manifest_digest": verification["manifest_digest"],
        "prediction_digest": prediction_digest,
        "reviewers": verification["reviewers"],
        "evaluation": result,
        "acceptance_split": split_report,
    }
    if baseline_predictions_path is not None:
        from .knowledge_eval import compare_evaluations

        baseline_predictions, baseline_digest = _load_jsonl(
            baseline_predictions_path,
            label="baseline prediction file",
            schema_name="knowledge-evaluation-prediction-v1.json",
        )
        baseline = evaluate_items(gold, baseline_predictions)
        report.update(
            {
                "baseline_prediction_digest": baseline_digest,
                "baseline_evaluation": baseline,
                "comparison": compare_evaluations(baseline, result),
            }
        )
    return report


def initialize_gold_reviews(
    *, candidates_path: Path, output_path: Path
) -> dict[str, Any]:
    """Create a private pending-review file bound to an exact candidate batch."""
    candidates, candidate_digest = _load_jsonl(
        candidates_path,
        label="candidate file",
        schema_name="knowledge-evaluation-candidate-v1.json",
    )
    reviews: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: str(item["id"])):
        suggestion = candidate["suggestion"]
        reviews.append(
            {
                "schema_version": 1,
                "candidate_id": candidate["id"],
                "candidate_digest": candidate_digest,
                "query_digest": _query_digest(str(candidate["query"])),
                "decision": "pending",
                "reviewed_by": None,
                "reviewed_at": None,
                "notes": "",
                "label": {
                    "expected_route": suggestion["expected_route"],
                    "answerable": suggestion["answerable"],
                    "allowed_knowledge_ids": suggestion["allowed_knowledge_ids"],
                    "allowed_claim_ids": suggestion["allowed_claim_ids"],
                    "forbidden_knowledge_ids": [],
                    "required_scope": {},
                    "clarification_allowed": suggestion["expected_route"] == "clarify",
                    "acceptable_abstention_reasons": suggestion[
                        "acceptable_abstention_reasons"
                    ],
                    "tags": suggestion["tags"],
                },
            }
        )
    payload = _encode_jsonl(reviews)
    output = Path(os.path.abspath(output_path.expanduser()))
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.exists() or output.is_symlink():
        if (
            not output.is_symlink()
            and output.is_file()
            and stat.S_IMODE(output.stat().st_mode) & 0o077 == 0
            and output.read_bytes() == payload
        ):
            status = "unchanged"
        else:
            raise KnowledgeGoldReviewError(
                f"refusing to overwrite review file: {output}"
            )
    else:
        _atomic_private_create(output, payload)
        status = "created"
    return {
        "output": str(output),
        "candidate_digest": candidate_digest,
        "review_count": len(reviews),
        "status": status,
        "next_action": "review every label, then set decision/reviewer/time",
    }


def promote_reviewed_candidates(
    *,
    candidates_path: Path,
    reviews_path: Path,
    output_dir: Path,
    allow_partial: bool = False,
) -> dict[str, Any]:
    """Create a private, immutable gold bundle from explicit human labels."""
    candidates, candidate_digest = _load_jsonl(
        candidates_path,
        label="candidate file",
        schema_name="knowledge-evaluation-candidate-v1.json",
    )
    reviews, _ = _load_jsonl(
        reviews_path,
        label="review file",
        schema_name="knowledge-evaluation-review-v1.json",
    )
    candidate_by_id = {str(item["id"]): item for item in candidates}
    all_review_by_id = {str(item["candidate_id"]): item for item in reviews}
    review_by_id = {
        candidate_id: item
        for candidate_id, item in all_review_by_id.items()
        if item["decision"] != "pending"
    }

    unknown = sorted(set(all_review_by_id) - set(candidate_by_id))
    if unknown:
        raise KnowledgeGoldReviewError(
            f"reviews reference unknown candidates: {unknown}"
        )
    for candidate_id, review in all_review_by_id.items():
        candidate = candidate_by_id[candidate_id]
        if review["candidate_digest"] != candidate_digest:
            raise KnowledgeGoldReviewError(
                f"{candidate_id}: candidate batch digest does not match"
            )
        if review["query_digest"] != _query_digest(str(candidate["query"])):
            raise KnowledgeGoldReviewError(
                f"{candidate_id}: query digest does not match"
            )
    unreviewed = sorted(set(candidate_by_id) - set(review_by_id))
    if unreviewed and not allow_partial:
        raise KnowledgeGoldReviewError(
            f"{len(unreviewed)} candidates are unreviewed; use --allow-partial explicitly"
        )

    gold: list[dict[str, Any]] = []
    normalized_reviews = [all_review_by_id[key] for key in sorted(all_review_by_id)]
    manifest_cases: list[dict[str, Any]] = []
    reviewers: set[str] = set()
    reviewed_times: list[datetime] = []
    rejected_count = 0
    for candidate_id in sorted(review_by_id):
        candidate = candidate_by_id[candidate_id]
        review = review_by_id[candidate_id]
        reviewed_at = _parse_reviewed_at(str(review["reviewed_at"]))
        reviewed_times.append(reviewed_at)
        reviewers.add(str(review["reviewed_by"]))
        if review["decision"] == "rejected":
            rejected_count += 1
            continue

        label = review["label"]
        if not isinstance(label, dict):
            raise KnowledgeGoldReviewError(
                f"{candidate_id}: accepted review needs a label"
            )
        _validate_final_label(candidate_id, label)
        item = {
            "id": candidate_id,
            "query": candidate["query"],
            **label,
        }
        errors = sorted(
            _validator("knowledge-evaluation-case-v1.json").iter_errors(item),
            key=lambda error: list(error.path),
        )
        if errors:
            error = errors[0]
            location = ".".join(str(part) for part in error.path) or "item"
            raise KnowledgeGoldReviewError(
                f"{candidate_id} final label {location}: {error.message}"
            )
        gold.append(item)
        manifest_cases.append(
            {
                "id": candidate_id,
                "reviewed_by": review["reviewed_by"],
                "reviewed_at": review["reviewed_at"],
                "source_type": candidate["provenance"]["source_type"],
                "source_id": candidate["provenance"]["source_id"],
            }
        )

    if not gold:
        raise KnowledgeGoldReviewError("at least one candidate must be accepted")
    from .knowledge_eval import KnowledgeEvaluationError, validate_gold_cases

    try:
        validate_gold_cases(gold)
    except KnowledgeEvaluationError as exc:
        raise KnowledgeGoldReviewError(str(exc)) from exc
    gold_payload = _encode_jsonl(gold)
    reviews_payload = _encode_jsonl(normalized_reviews)
    gold_digest = hashlib.sha256(gold_payload).hexdigest()
    review_digest = hashlib.sha256(reviews_payload).hexdigest()
    manifest = {
        "schema_version": 1,
        "candidate_digest": candidate_digest,
        "review_digest": review_digest,
        "gold_digest": gold_digest,
        "candidate_count": len(candidates),
        "reviewed_count": len(review_by_id),
        "accepted_count": len(gold),
        "rejected_count": rejected_count,
        "unreviewed_count": len(unreviewed),
        "complete": not unreviewed,
        "reviewers": sorted(reviewers),
        "reviewed_at_min": min(reviewed_times).isoformat(),
        "reviewed_at_max": max(reviewed_times).isoformat(),
        "cases": manifest_cases,
    }
    manifest_errors = sorted(
        _validator("knowledge-evaluation-gold-manifest-v1.json").iter_errors(manifest),
        key=lambda error: list(error.path),
    )
    if manifest_errors:
        raise KnowledgeGoldReviewError(manifest_errors[0].message)
    manifest_payload = (canonical_json(manifest) + "\n").encode("utf-8")
    manifest_digest = hashlib.sha256(manifest_payload).hexdigest()
    expected = {
        "gold.jsonl": gold_payload,
        "reviews.jsonl": reviews_payload,
        "manifest.json": manifest_payload,
    }

    output = Path(os.path.abspath(output_dir.expanduser()))
    if output.exists() or output.is_symlink():
        if _existing_bundle_matches(output, expected):
            return {
                **manifest,
                "manifest_digest": manifest_digest,
                "output": str(output),
                "status": "unchanged",
            }
        raise KnowledgeGoldReviewError(f"refusing to overwrite gold bundle: {output}")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".gold-partial-", dir=output.parent))
    output_created = False
    try:
        for name, payload in expected.items():
            _private_write(temporary / name, payload)
        directory_fd = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        try:
            os.mkdir(output, 0o700)
            output_created = True
        except FileExistsError as exc:
            raise KnowledgeGoldReviewError(
                f"refusing to overwrite gold bundle: {output}"
            ) from exc
        for name in expected:
            os.rename(temporary / name, output / name)
        output_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(output_fd)
        finally:
            os.close(output_fd)
        parent_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except Exception:
        if output_created and output.exists() and output.is_dir():
            shutil.rmtree(output)
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {
        **manifest,
        "manifest_digest": manifest_digest,
        "output": str(output),
        "status": "created",
    }
