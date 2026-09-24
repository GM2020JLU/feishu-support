from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from .db import transaction
from .ids import canonical_json, digest
from .knowledge import DISCLOSURE_LEVELS, register_source
from .timeutil import iso_now


class ProfessionalKnowledgeError(ValueError):
    pass


GitBlobReader = Callable[[Path, str, str], bytes]


SCHEMA_VERSION = 2
VALIDATION_LAYERS = {
    "static",
    "build",
    "ram_boot",
    "persistent_flash",
    "device_function",
    "stability",
}
_DISCLOSURE_RANK = {
    "public": 0,
    "internal": 1,
    "team": 2,
    "private": 3,
    "restricted": 4,
}
_LOCAL_PATH_RE = re.compile(r"(?:^|[\s`])(\/home\/|\/Users\/|[A-Za-z]:\\)")


class _FrontmatterLoader(yaml.SafeLoader):
    pass


_FrontmatterLoader.yaml_implicit_resolvers = {
    key: [
        resolver for resolver in value if resolver[0] != "tag:yaml.org,2002:timestamp"
    ]
    for key, value in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


@dataclass(frozen=True)
class KnowledgeArticle:
    path: Path
    metadata: dict[str, Any]
    body_markdown: str
    revision_digest: str

    @property
    def stable_id(self) -> str:
        return str(self.metadata["id"])

    @property
    def revision(self) -> int:
        return int(self.metadata["revision"])


def _schema() -> dict[str, Any]:
    path = resources.files("k3_support").joinpath(
        "schemas/professional-knowledge-v2.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProfessionalKnowledgeError(f"{field} is not a valid date-time") from exc
    if parsed.tzinfo is None:
        raise ProfessionalKnowledgeError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def revision_digest(metadata: dict[str, Any], body_markdown: str) -> str:
    """Digest every publishable field while excluding the digest's own slot."""
    normalized = copy.deepcopy(metadata)
    review = normalized.get("review")
    if isinstance(review, dict):
        review.pop("approved_revision_digest", None)
    return digest(
        {
            "schema_version": SCHEMA_VERSION,
            "metadata": normalized,
            "body_markdown": body_markdown.strip(),
        }
    )


def _parse_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise ProfessionalKnowledgeError(f"article must be a regular file: {path}")
    raw = path.read_text(encoding="utf-8")
    return parse_article_text(raw, label=str(path))


def parse_article_text(raw: str, *, label: str = 'in-memory article') -> tuple[dict[str, Any], str]:
    """Use the same bounded frontmatter parser for files and captured drafts."""
    path = label
    if not isinstance(raw, str):
        raise ProfessionalKnowledgeError('article text must be a string')
    if len(raw.encode("utf-8")) > 1_000_000:
        raise ProfessionalKnowledgeError(f"article exceeds 1 MiB: {path}")
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ProfessionalKnowledgeError(f"article has no YAML frontmatter: {path}")
    try:
        boundary = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as exc:
        raise ProfessionalKnowledgeError(
            f"article has unterminated YAML frontmatter: {path}"
        ) from exc
    try:
        metadata = yaml.load("\n".join(lines[1:boundary]), Loader=_FrontmatterLoader)
    except yaml.YAMLError as exc:
        raise ProfessionalKnowledgeError(f"invalid YAML frontmatter: {path}") from exc
    body = "\n".join(lines[boundary + 1 :]).strip()
    if not isinstance(metadata, dict):
        raise ProfessionalKnowledgeError(f"frontmatter must be an object: {path}")
    if not body:
        raise ProfessionalKnowledgeError(f"article body is empty: {path}")
    return metadata, body


def validate_article(
    metadata: dict[str, Any],
    body_markdown: str,
    *,
    now: datetime | None = None,
) -> str:
    validator = Draft202012Validator(_schema(), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(metadata), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.path) or "article"
        raise ProfessionalKnowledgeError(f"{location}: {first.message}")

    calculated = revision_digest(metadata, body_markdown)
    claim_ids = [str(item["id"]) for item in metadata["claims"]]
    source_ids = [str(item["id"]) for item in metadata["sources"]]
    validation_ids = [str(item["id"]) for item in metadata["validation"]]
    for label, values in (
        ("claim", claim_ids),
        ("source", source_ids),
        ("validation", validation_ids),
    ):
        if len(values) != len(set(values)):
            raise ProfessionalKnowledgeError(f"duplicate {label} id")

    source_by_id = {str(item["id"]): item for item in metadata["sources"]}
    claim_by_id = {str(item["id"]): item for item in metadata["claims"]}
    for claim in metadata["claims"]:
        missing = set(claim["source_refs"]) - set(source_by_id)
        if missing:
            raise ProfessionalKnowledgeError(
                f"claim {claim['id']} references unknown sources: {sorted(missing)}"
            )
    for item in metadata["validation"]:
        missing = set(item["claim_refs"]) - set(claim_by_id)
        if missing:
            raise ProfessionalKnowledgeError(
                f"validation {item['id']} references unknown claims: {sorted(missing)}"
            )

    scope = metadata["scope"]
    if scope["basis"] == "hardware_specific" and not scope["boards"]:
        raise ProfessionalKnowledgeError(
            "hardware_specific scope requires at least one explicit board"
        )
    if scope["basis"] == "software_only" and scope["boards"]:
        raise ProfessionalKnowledgeError(
            "software_only scope must not claim board applicability"
        )
    if any(
        str(value).lower() in {"unknown", "any", "*"}
        for value in scope["software_versions"]
    ):
        raise ProfessionalKnowledgeError(
            "software_versions must be explicit; unknown/any/wildcard are forbidden"
        )

    publication = metadata["publication"]
    if publication["answer_visibility"] not in DISCLOSURE_LEVELS:
        raise ProfessionalKnowledgeError("invalid answer visibility")
    for source in metadata["sources"]:
        if (
            _DISCLOSURE_RANK[publication["source_body_visibility"]]
            < _DISCLOSURE_RANK[source["visibility"]]
        ):
            raise ProfessionalKnowledgeError(
                f"source_body_visibility is broader than source {source['id']}"
            )
        if (
            _DISCLOSURE_RANK[publication["answer_visibility"]]
            < _DISCLOSURE_RANK[source["visibility"]]
        ):
            link_only = (
                metadata["kind"] == "document_route"
                and source["share_mode"] == "link_only"
            )
            if not link_only:
                raise ProfessionalKnowledgeError(
                    f"answer visibility is broader than source {source['id']}"
                )

    kind = metadata["kind"]
    content = metadata["content"]
    if kind in {"procedure", "validation_recipe"}:
        for field in ("steps", "expected_observations", "rollback"):
            if not content[field]:
                raise ProfessionalKnowledgeError(f"{kind} requires content.{field}")
    if kind == "command_reference" and not content.get("commands"):
        raise ProfessionalKnowledgeError("command_reference requires content.commands")
    if kind == "document_route" and publication["link_policy"] == "never":
        raise ProfessionalKnowledgeError("document_route cannot use link_policy=never")

    passed: dict[str, set[str]] = {claim_id: set() for claim_id in claim_ids}
    for item in metadata["validation"]:
        if item["result"] != "passed":
            continue
        for claim_id in item["claim_refs"]:
            passed[claim_id].add(item["layer"])
    for claim_id, claim in claim_by_id.items():
        missing = set(claim["required_validation"]) - passed[claim_id]
        if metadata["status"] == "published" and missing:
            raise ProfessionalKnowledgeError(
                f"claim {claim_id} lacks passed validation: {sorted(missing)}"
            )
        if claim["risk_class"] in {"persistent", "destructive"} and (
            not content["warnings"] or not content["rollback"]
        ):
            raise ProfessionalKnowledgeError(
                f"claim {claim_id} requires warnings and rollback"
            )

    if metadata["status"] == "published":
        if metadata["kind"] == "unclassified":
            raise ProfessionalKnowledgeError(
                "published article must have a knowledge kind"
            )
        if metadata["owner"] is None:
            raise ProfessionalKnowledgeError("published article requires an owner")
        if scope["basis"] == "unresolved":
            raise ProfessionalKnowledgeError("published article has unresolved scope")
        if any(value == "unresolved" for value in scope["software_versions"]):
            raise ProfessionalKnowledgeError("published article has unresolved version")
        review = metadata["review"]
        if not isinstance(review, dict):
            raise ProfessionalKnowledgeError(
                "published article requires review metadata"
            )
        missing_snapshots = [
            source["id"]
            for source in metadata["sources"]
            if source["snapshot_digest"] is None
        ]
        if missing_snapshots:
            raise ProfessionalKnowledgeError(
                f"published sources lack snapshot digests: {missing_snapshots}"
            )
        if review["approved_revision_digest"] != calculated:
            raise ProfessionalKnowledgeError(
                f"approved_revision_digest does not match revision: expected {calculated}"
            )
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if _parse_time(review["review_due_at"], "review.review_due_at") <= current:
            raise ProfessionalKnowledgeError("published review is expired")
        if publication["automatic_reply"]:
            quality = metadata.get("quality")
            if not isinstance(quality, dict):
                raise ProfessionalKnowledgeError(
                    "automatic_reply requires calibrated quality evidence"
                )
            if (
                int(quality["sample_size"]) < 30
                or float(quality["direct_answer_precision"]) < 0.99
                or float(quality["answerable_recall_at_5"]) < 0.95
                or float(quality["abstention_recall"]) < 0.98
            ):
                raise ProfessionalKnowledgeError(
                    "automatic_reply quality gate is not satisfied"
                )
        if _LOCAL_PATH_RE.search(body_markdown):
            raise ProfessionalKnowledgeError(
                "published body contains a local absolute path"
            )
    return calculated


def load_article(path: Path, *, now: datetime | None = None) -> KnowledgeArticle:
    metadata, body = _parse_frontmatter(path)
    calculated = validate_article(metadata, body, now=now)
    return KnowledgeArticle(path.resolve(), metadata, body, calculated)


def calculate_article_digest(path: Path) -> dict[str, Any]:
    metadata, body = _parse_frontmatter(path.expanduser().resolve())
    return {
        "id": metadata.get("id"),
        "revision": metadata.get("revision"),
        "revision_digest": revision_digest(metadata, body),
    }


def lint_repository(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    root = root.expanduser().resolve()
    vocabulary_path = root / "vocabulary.yaml"
    if vocabulary_path.is_symlink() or not vocabulary_path.is_file():
        raise ProfessionalKnowledgeError(
            f"missing regular vocabulary file: {vocabulary_path}"
        )
    vocabulary = yaml.safe_load(vocabulary_path.read_text(encoding="utf-8"))
    required_vocabularies = {
        "products",
        "components",
        "boards",
        "boot_stages",
        "storage_media",
    }
    if (
        not isinstance(vocabulary, dict)
        or vocabulary.get("schema_version") != SCHEMA_VERSION
        or not required_vocabularies.issubset(vocabulary)
    ):
        raise ProfessionalKnowledgeError("invalid controlled vocabulary")
    for name in required_vocabularies:
        values = vocabulary[name]
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) or not value.strip() for value in values)
            or len(values) != len(set(values))
        ):
            raise ProfessionalKnowledgeError(f"invalid vocabulary list: {name}")
    articles_root = root / "articles"
    if articles_root.is_symlink() or not articles_root.is_dir():
        raise ProfessionalKnowledgeError(f"missing articles directory: {articles_root}")
    # Fail explicitly rather than silently omitting a linked article/directory
    # from a release. Do not traverse links outside the reviewed repository.
    entries = sorted(articles_root.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise ProfessionalKnowledgeError("linked entries are not allowed in articles")
    paths = [path for path in entries if path.suffix == ".md"]
    if not paths:
        raise ProfessionalKnowledgeError("knowledge repository has no articles")
    articles = [load_article(path, now=now) for path in paths]
    revisions: set[tuple[str, int]] = set()
    published: set[str] = set()
    for article in articles:
        key = (article.stable_id, article.revision)
        if key in revisions:
            raise ProfessionalKnowledgeError(
                f"duplicate knowledge revision: {article.stable_id}@{article.revision}"
            )
        revisions.add(key)
        scope = article.metadata["scope"]
        for field, vocabulary_name in (
            ("product", "products"),
            ("component", "components"),
        ):
            if scope[field] not in vocabulary[vocabulary_name]:
                raise ProfessionalKnowledgeError(
                    f"{article.stable_id} uses unknown {field}: {scope[field]}"
                )
        for field, vocabulary_name in (
            ("boards", "boards"),
            ("boot_stages", "boot_stages"),
            ("storage_media", "storage_media"),
        ):
            unknown = set(scope.get(field, [])) - set(vocabulary[vocabulary_name])
            if unknown:
                raise ProfessionalKnowledgeError(
                    f"{article.stable_id} uses unknown {field}: {sorted(unknown)}"
                )
        if article.metadata["status"] == "published":
            if article.stable_id in published:
                raise ProfessionalKnowledgeError(
                    f"multiple published revisions: {article.stable_id}"
                )
            published.add(article.stable_id)
    return {
        "root": str(root),
        "article_count": len(articles),
        "published_count": len(published),
        "vocabulary": vocabulary,
        "articles": articles,
    }


def _read_git_blob(checkout: Path, commit: str, path: str) -> bytes:
    process = subprocess.run(
        ["git", "-C", str(checkout), "show", f"{commit}:{path}"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if process.returncode != 0:
        raise ProfessionalKnowledgeError(f"cannot read Git blob {commit}:{path}")
    if len(process.stdout) > 20 * 1024 * 1024:
        raise ProfessionalKnowledgeError(f"Git blob exceeds 20 MiB: {path}")
    return process.stdout


def verify_git_sources(
    repository_root: Path,
    *,
    repository: str,
    checkout: Path,
    blob_reader: GitBlobReader | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Recompute source digests from immutable Git locators without exposing bodies."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository):
        raise ProfessionalKnowledgeError("repository name is invalid")
    resolved_checkout = checkout.expanduser().resolve()
    if blob_reader is None:
        if not resolved_checkout.is_dir():
            raise ProfessionalKnowledgeError("Git checkout is not a directory")
        top = subprocess.run(
            ["git", "-C", str(resolved_checkout), "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if (
            top.returncode != 0
            or Path(top.stdout.strip()).resolve() != resolved_checkout
        ):
            raise ProfessionalKnowledgeError("checkout is not the exact Git top level")
    reader = blob_reader or _read_git_blob
    report = lint_repository(repository_root, now=now)
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    expected_by_source: dict[tuple[str, str], str | None] = {}
    for article in report["articles"]:
        for source in article.metadata["sources"]:
            locator = source["locator"]
            if source["type"] != "git" or locator.get("repository") != repository:
                continue
            commit = locator.get("commit")
            path = locator.get("path")
            if (
                not isinstance(commit, str)
                or not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit)
                or not isinstance(path, str)
                or not path
                or path.startswith("/")
                or ".." in path.split("/")
                or "\x00" in path
            ):
                raise ProfessionalKnowledgeError(
                    f"invalid Git locator in {article.stable_id}:{source['id']}"
                )
            if source["version"].lower() != commit.lower():
                raise ProfessionalKnowledgeError(
                    f"source version differs from locator commit: {article.stable_id}:{source['id']}"
                )
            key = (commit.lower(), path)
            if key in seen:
                if expected_by_source[key] != source["snapshot_digest"]:
                    raise ProfessionalKnowledgeError(
                        f"conflicting snapshot digests for {commit}:{path}"
                    )
                continue
            seen.add(key)
            expected_by_source[key] = source["snapshot_digest"]
            blob = reader(resolved_checkout, commit.lower(), path)
            if len(blob) > 20 * 1024 * 1024:
                raise ProfessionalKnowledgeError(f"Git blob exceeds 20 MiB: {path}")
            actual = hashlib.sha256(blob).hexdigest()
            expected = source["snapshot_digest"]
            results.append(
                {
                    "commit": commit.lower(),
                    "path": path,
                    "expected_digest": expected,
                    "actual_digest": actual,
                    "matches": actual == expected,
                }
            )
    mismatches = [item for item in results if not item["matches"]]
    return {
        "repository": repository,
        "checkout": str(resolved_checkout),
        "source_count": len(results),
        "mismatch_count": len(mismatches),
        "ok": bool(results) and not mismatches,
        "sources": results,
    }


def compile_repository(root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    report = lint_repository(root, now=now)
    entries = []
    for article in report["articles"]:
        if article.metadata["status"] != "published":
            continue
        entries.append(
            {
                "metadata": article.metadata,
                "body_markdown": article.body_markdown,
                "revision_digest": article.revision_digest,
            }
        )
    entries.sort(
        key=lambda item: (item["metadata"]["id"], item["metadata"]["revision"])
    )
    payload = {"schema_version": SCHEMA_VERSION, "entries": entries}
    return {**payload, "bundle_digest": digest(payload)}


def _atomic_write_json(payload: dict[str, Any], output_path: Path) -> Path:
    output_path = output_path.expanduser()
    if output_path.is_symlink():
        raise ProfessionalKnowledgeError("bundle output must not be a symlink")
    output_path = output_path.resolve()
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, output_path)
    output_path.chmod(0o600)
    return output_path


def write_bundle(bundle: dict[str, Any], output_path: Path) -> dict[str, Any]:
    output_path = _atomic_write_json(bundle, output_path)
    return {
        "output": str(output_path),
        "entry_count": len(bundle["entries"]),
        "bundle_digest": bundle["bundle_digest"],
    }


def legacy_inventory(conn: sqlite3.Connection) -> dict[str, Any]:
    """Inventory legacy rows without guessing professional scope or evidence."""
    entries: list[dict[str, Any]] = []
    missing_counts: dict[str, int] = {
        "hardware": 0,
        "applicability": 0,
        "owner": 0,
        "review_due_at": 0,
        "evidence_layers": 0,
        "source_snapshot_digest": 0,
    }
    for row in conn.execute(
        """SELECT * FROM knowledge_entries
             WHERE status IN ('approved','candidate','stale')
             ORDER BY status,title,knowledge_id"""
    ):
        sources: list[dict[str, Any]] = []
        for source in conn.execute(
            """SELECT ks.source_type,ks.stable_external_id,ks.url,ks.source_version,
                      ks.visibility,ks.claim,sr.title,sr.content_digest,sr.updated_at,
                      sr.acl_json
                 FROM knowledge_sources ks
                 LEFT JOIN source_registry sr
                   ON sr.source_type=ks.source_type
                  AND sr.stable_external_id=ks.stable_external_id
                WHERE ks.knowledge_id=?
                ORDER BY ks.source_type,ks.stable_external_id,ks.claim""",
            (row["knowledge_id"],),
        ):
            item = dict(source)
            item["acl"] = json.loads(item.pop("acl_json") or "{}")
            sources.append(item)
        missing: list[str] = []
        for field in ("hardware", "applicability", "owner", "review_due_at"):
            if not row[field]:
                missing.append(field)
                missing_counts[field] += 1
        if not json.loads(row["evidence_layers_json"]):
            missing.append("evidence_layers")
            missing_counts["evidence_layers"] += 1
        if not sources or any(not source["content_digest"] for source in sources):
            missing.append("source_snapshot_digest")
            missing_counts["source_snapshot_digest"] += 1
        entries.append(
            {
                "legacy_knowledge_id": row["knowledge_id"],
                "status": row["status"],
                "title": row["title"],
                "questions": json.loads(row["question_variants_json"]),
                "answer_markdown": row["answer_markdown"],
                "project": row["project"],
                "module": row["module"],
                "hardware": row["hardware"],
                "software_version": row["software_version"],
                "applicability": row["applicability"],
                "disclosure_class": row["disclosure_class"],
                "owner": row["owner"],
                "reviewed_by": row["reviewed_by"],
                "reviewed_at": row["reviewed_at"],
                "review_due_at": row["review_due_at"],
                "evidence_layers": json.loads(row["evidence_layers_json"]),
                "sources": sources,
                "missing_professional_fields": missing,
            }
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": "legacy_knowledge_inventory",
        "entry_count": len(entries),
        "missing_counts": missing_counts,
        "entries": entries,
    }
    return {**payload, "inventory_digest": digest(payload)}


def write_legacy_inventory(
    conn: sqlite3.Connection, *, output_path: Path
) -> dict[str, Any]:
    inventory = legacy_inventory(conn)
    output_path = _atomic_write_json(inventory, output_path)
    return {
        "output": str(output_path),
        "entry_count": inventory["entry_count"],
        "inventory_digest": inventory["inventory_digest"],
        "missing_counts": inventory["missing_counts"],
    }


def export_legacy_drafts(
    conn: sqlite3.Connection, *, repository_root: Path
) -> dict[str, Any]:
    """Create captured drafts while preserving every unresolved legacy field."""
    inventory = legacy_inventory(conn)
    repository_root = repository_root.expanduser().resolve()
    articles_root = repository_root / "articles" / "legacy"
    articles_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    written: list[str] = []
    unchanged: list[str] = []
    for entry in inventory["entries"]:
        suffix = str(entry["legacy_knowledge_id"]).removeprefix("knw_")
        stable_id = f"k3.legacy.{suffix}"
        sources: list[dict[str, Any]] = []
        for index, source in enumerate(entry["sources"], start=1):
            acl = source["acl"]
            sources.append(
                {
                    "id": f"source-{index}",
                    "type": source["source_type"],
                    "stable_external_id": source["stable_external_id"],
                    "title": source["title"],
                    "url": source["url"],
                    "version": source["source_version"] or "unresolved",
                    "snapshot_digest": source["content_digest"],
                    "authority": 0.0,
                    "visibility": source["visibility"],
                    "share_mode": (
                        "link_only" if acl.get("link_only") else "full_answer"
                    ),
                    "locator": {
                        "legacy_claim": source["claim"],
                        "stable_external_id": source["stable_external_id"],
                    },
                }
            )
        if not sources:
            sources.append(
                {
                    "id": "legacy-record",
                    "type": "legacy_database",
                    "stable_external_id": entry["legacy_knowledge_id"],
                    "title": entry["title"],
                    "url": None,
                    "version": "unresolved",
                    "snapshot_digest": None,
                    "authority": 0.0,
                    "visibility": "private",
                    "share_mode": "never",
                    "locator": {"legacy_knowledge_id": entry["legacy_knowledge_id"]},
                }
            )
        questions = list(
            dict.fromkeys(
                str(value) for value in entry["questions"] if str(value).strip()
            )
        )
        source_visibility = max(
            (source["visibility"] for source in sources),
            key=_DISCLOSURE_RANK.__getitem__,
        )
        legacy_visibility = entry["disclosure_class"]
        answer_visibility = max(
            (source_visibility, legacy_visibility), key=_DISCLOSURE_RANK.__getitem__
        )
        metadata: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "id": stable_id,
            "revision": 1,
            "kind": "unclassified",
            "status": "captured",
            "title": entry["title"],
            "owner": None,
            "scope": {
                "product": entry["project"] or "K3",
                "component": "unclassified",
                "subcomponent": entry["module"],
                "basis": "unresolved",
                "boards": [],
                "hardware_revisions": [],
                "software_versions": [entry["software_version"] or "unresolved"],
                "boot_stages": [],
                "storage_media": [],
                "operating_systems": [],
            },
            "intent": {
                "aliases": questions,
                "question_examples": questions,
                "required_entities": [],
                "negative_constraints": [],
            },
            "content": {
                "summary": entry["answer_markdown"],
                "prerequisites": [],
                "steps": [],
                "expected_observations": [],
                "failure_branches": [],
                "rollback": [],
                "warnings": [],
                "commands": [],
            },
            "claims": [
                {
                    "id": "legacy-answer",
                    "statement": entry["answer_markdown"],
                    "source_refs": [source["id"] for source in sources],
                    "risk_class": "read_only",
                    "required_validation": ["static"],
                }
            ],
            "sources": sources,
            "validation": [],
            "publication": {
                "answer_visibility": answer_visibility,
                "source_body_visibility": source_visibility,
                "link_policy": (
                    "request_if_denied"
                    if any(source["share_mode"] == "link_only" for source in sources)
                    else "direct"
                ),
                "automatic_reply": False,
                "allowed_chat_ids": [],
                "allowed_user_ids": [],
            },
            "quality": None,
            "review": None,
            "migration": {
                "legacy_knowledge_id": entry["legacy_knowledge_id"],
                "unresolved_fields": entry["missing_professional_fields"],
            },
        }
        body = entry["answer_markdown"].strip()
        validate_article(metadata, body)
        rendered = (
            "---\n"
            + yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True)
            + "---\n\n"
            + body
            + "\n"
        )
        path = articles_root / f"{suffix}-r1.md"
        if path.exists():
            if path.read_text(encoding="utf-8") != rendered:
                raise ProfessionalKnowledgeError(
                    f"legacy draft already has local edits: {path}"
                )
            unchanged.append(str(path))
            continue
        path.write_text(rendered, encoding="utf-8")
        path.chmod(0o600)
        written.append(str(path))
    return {
        "inventory_digest": inventory["inventory_digest"],
        "entry_count": inventory["entry_count"],
        "written": written,
        "unchanged": unchanged,
    }


def load_bundle(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    path = path.expanduser()
    if path.is_symlink() or not path.is_file():
        raise ProfessionalKnowledgeError("bundle must be a regular file")
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfessionalKnowledgeError("bundle is not valid JSON") from exc
    return validate_bundle(bundle, now=now)


def validate_bundle(bundle, *, now=None):
    """Validate a supplied bundle using the same gates as the file importer."""
    if not isinstance(bundle, dict) or set(bundle) != {
        "schema_version",
        "entries",
        "bundle_digest",
    }:
        raise ProfessionalKnowledgeError("invalid professional bundle envelope")
    if bundle["schema_version"] != SCHEMA_VERSION or not isinstance(
        bundle["entries"], list
    ):
        raise ProfessionalKnowledgeError("unsupported professional bundle schema")
    payload = {"schema_version": SCHEMA_VERSION, "entries": bundle["entries"]}
    if bundle["bundle_digest"] != digest(payload):
        raise ProfessionalKnowledgeError("professional bundle digest mismatch")
    seen: set[str] = set()
    source_bindings = {}
    for entry in bundle["entries"]:
        if not isinstance(entry, dict) or set(entry) != {
            "metadata",
            "body_markdown",
            "revision_digest",
        }:
            raise ProfessionalKnowledgeError("invalid professional bundle entry")
        calculated = validate_article(
            entry["metadata"], entry["body_markdown"], now=now
        )
        if calculated != entry["revision_digest"]:
            raise ProfessionalKnowledgeError("professional revision digest mismatch")
        key = entry["metadata"]["id"]
        if key in seen:
            raise ProfessionalKnowledgeError("duplicate professional bundle article; select one revision per article")
        seen.add(key)
        for source in entry["metadata"]["sources"]:
            coordinate = (source["type"], source["stable_external_id"])
            binding = tuple(source.get(field) for field in ("version", "snapshot_digest", "visibility", "url"))
            if coordinate in source_bindings and source_bindings[coordinate] != binding:
                raise ProfessionalKnowledgeError("conflicting source snapshots in professional bundle")
            source_bindings[coordinate] = binding
    return bundle


def plan_import(
    conn: sqlite3.Connection,
    *,
    bundle_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    bundle = load_bundle(bundle_path, now=now)
    return _plan_loaded_import(conn, bundle)


def _plan_loaded_import(conn, bundle):
    actions = {"create": 0, "update": 0, "unchanged": 0}
    for entry in bundle["entries"]:
        stable_id = entry["metadata"]["id"]
        existing = conn.execute(
            """SELECT pkr.revision_number,pkr.revision_digest
                 FROM knowledge_entries ke
                 JOIN professional_knowledge_revisions pkr
                   ON pkr.revision_id=ke.professional_revision_id
                WHERE pkr.stable_id=?""",
            (stable_id,),
        ).fetchone()
        if existing is None:
            actions["create"] += 1
        elif (
            int(existing["revision_number"]) == int(entry["metadata"]["revision"])
            and existing["revision_digest"] == entry["revision_digest"]
        ):
            actions["unchanged"] += 1
        elif int(existing["revision_number"]) < int(entry["metadata"]["revision"]):
            actions["update"] += 1
        else:
            raise ProfessionalKnowledgeError(
                f"bundle would downgrade or conflict with {stable_id}"
            )
    return {
        "bundle_digest": bundle["bundle_digest"],
        "entry_count": len(bundle["entries"]),
        "actions": actions,
    }


def _projection_id(stable_id: str) -> str:
    return f"knw_{digest({'professional_id': stable_id})[:32]}"


def answer_trust_context(
    conn: sqlite3.Connection, *, knowledge_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT pkr.stable_id,pkr.revision_number,pkr.revision_digest,
                  pkr.payload_json,pkr.reviewed_at,pkr.review_due_at
             FROM knowledge_entries ke
             JOIN professional_knowledge_revisions pkr
               ON pkr.revision_id=ke.professional_revision_id
            WHERE ke.knowledge_id=? AND ke.status='approved'
              AND pkr.lifecycle_state='published'""",
        (knowledge_id,),
    ).fetchone()
    if row is None:
        return None
    metadata = json.loads(row["payload_json"])
    sources = [
        {
            "id": source["id"],
            "title": source.get("title"),
            "url": source.get("url"),
            "share_mode": source["share_mode"],
        }
        for source in metadata["sources"]
        if source["share_mode"] != "never"
    ]
    return {
        "stable_id": row["stable_id"],
        "revision": int(row["revision_number"]),
        "revision_digest": row["revision_digest"],
        "scope": metadata["scope"],
        "validation_layers": sorted(
            {
                validation["layer"]
                for validation in metadata["validation"]
                if validation["result"] == "passed"
            }
        ),
        "reviewed_at": row["reviewed_at"],
        "review_due_at": row["review_due_at"],
        "sources": sources,
    }


def import_bundle(
    conn: sqlite3.Connection,
    *,
    bundle_path: Path,
    approved_digest: str,
    reviewer_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not reviewer_id.strip():
        raise ProfessionalKnowledgeError("reviewer_id is required")
    bundle = load_bundle(bundle_path, now=now)
    if approved_digest != bundle["bundle_digest"]:
        raise ProfessionalKnowledgeError("approved digest does not match bundle")
    with transaction(conn):
        return _import_loaded_bundle(conn, bundle, approved_digest, reviewer_id)


def _import_loaded_bundle(conn, bundle, approved_digest, reviewer_id):
    from .knowledge import _source_transaction

    plan = _plan_loaded_import(conn, bundle)
    imported_at = iso_now()
    unchanged_ids: set[str] = set()

    for entry in bundle["entries"]:
        metadata = entry["metadata"]
        existing = conn.execute(
            """SELECT pkr.revision_number,pkr.revision_digest
                 FROM knowledge_entries ke
                 JOIN professional_knowledge_revisions pkr
                   ON pkr.revision_id=ke.professional_revision_id
                WHERE pkr.stable_id=?""",
            (metadata["id"],),
        ).fetchone()
        if (
            existing is not None
            and int(existing["revision_number"]) == int(metadata["revision"])
            and existing["revision_digest"] == entry["revision_digest"]
        ):
            unchanged_ids.add(str(metadata["id"]))
            continue
        for source in metadata["sources"]:
            register_source(
                conn,
                source_type=source["type"],
                stable_external_id=source["stable_external_id"],
                title=source.get("title"),
                url=source.get("url"),
                acl={"visibility": source["visibility"]},
                source_version=source["version"],
                content_digest=source["snapshot_digest"],
                updated_at=metadata["review"]["reviewed_at"],
            )

    imported: list[str] = []
    with _source_transaction(conn):
        for entry in bundle["entries"]:
            metadata = entry["metadata"]
            stable_id = str(metadata["id"])
            revision_number = int(metadata["revision"])
            revision_id = f"kvr_{entry['revision_digest'][:32]}"
            knowledge_id = _projection_id(stable_id)
            if stable_id in unchanged_ids:
                imported.append(knowledge_id)
                continue
            questions = list(metadata["intent"]["question_examples"])
            for alias in metadata["intent"]["aliases"]:
                if alias not in questions:
                    questions.append(alias)
            scope = metadata["scope"]
            publication = metadata["publication"]
            authorities = [float(source["authority"]) for source in metadata["sources"]]
            passed_layers = sorted(
                {
                    item["layer"]
                    for item in metadata["validation"]
                    if item["result"] == "passed"
                }
            )
            software_versions = scope["software_versions"]
            software_version = (
                software_versions[0] if len(software_versions) == 1 else None
            )
            content_digest_value = entry["revision_digest"]
            confidence = 0.99 if publication["automatic_reply"] else 0.0

            conn.execute(
                """INSERT INTO knowledge_entries(
                       knowledge_id,title,status,question_variants_json,answer_markdown,
                       project,module,hardware,software_version,applicability,
                       disclosure_class,allowed_chat_ids_json,allowed_user_ids_json,
                       confidence,source_authority,evidence_layers_json,owner,
                       reviewed_by,reviewed_at,review_due_at,source_digest,content_digest,
                       canonical_case_id,created_at,updated_at,professional_revision_id)
                   VALUES(?,?,'approved',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)
                   ON CONFLICT(knowledge_id) DO UPDATE SET
                       title=excluded.title,status='approved',
                       question_variants_json=excluded.question_variants_json,
                       answer_markdown=excluded.answer_markdown,project=excluded.project,
                       module=excluded.module,hardware=excluded.hardware,
                       software_version=excluded.software_version,
                       applicability=excluded.applicability,
                       disclosure_class=excluded.disclosure_class,
                       allowed_chat_ids_json=excluded.allowed_chat_ids_json,
                       allowed_user_ids_json=excluded.allowed_user_ids_json,
                       confidence=excluded.confidence,
                       source_authority=excluded.source_authority,
                       evidence_layers_json=excluded.evidence_layers_json,
                       owner=excluded.owner,reviewed_by=excluded.reviewed_by,
                       reviewed_at=excluded.reviewed_at,review_due_at=excluded.review_due_at,
                       source_digest=excluded.source_digest,content_digest=excluded.content_digest,
                       updated_at=excluded.updated_at,
                       professional_revision_id=excluded.professional_revision_id""",
                (
                    knowledge_id,
                    metadata["title"],
                    canonical_json(questions),
                    entry["body_markdown"],
                    scope["product"],
                    scope["component"],
                    canonical_json(scope["boards"]),
                    software_version,
                    canonical_json(scope),
                    publication["answer_visibility"],
                    canonical_json(publication["allowed_chat_ids"]),
                    canonical_json(publication["allowed_user_ids"]),
                    confidence,
                    min(authorities),
                    canonical_json(passed_layers),
                    metadata["owner"],
                    metadata["review"]["reviewed_by"],
                    metadata["review"]["reviewed_at"],
                    metadata["review"]["review_due_at"],
                    content_digest_value,
                    content_digest_value,
                    imported_at,
                    imported_at,
                    revision_id,
                ),
            )
            conn.execute(
                """INSERT OR IGNORE INTO professional_knowledge_revisions(
                       revision_id,stable_id,revision_number,knowledge_id,kind,
                       lifecycle_state,revision_digest,payload_json,body_markdown,owner,
                       reviewed_by,reviewed_at,review_due_at,imported_at)
                   VALUES(?,?,?,?,?,'published',?,?,?,?,?,?,?,?)""",
                (
                    revision_id,
                    stable_id,
                    revision_number,
                    knowledge_id,
                    metadata["kind"],
                    entry["revision_digest"],
                    canonical_json(metadata),
                    entry["body_markdown"],
                    metadata["owner"],
                    metadata["review"]["reviewed_by"],
                    metadata["review"]["reviewed_at"],
                    metadata["review"]["review_due_at"],
                    imported_at,
                ),
            )
            conn.execute(
                """UPDATE professional_knowledge_revisions
                      SET lifecycle_state='retired'
                    WHERE stable_id=? AND revision_id<>?
                      AND lifecycle_state IN ('published','needs_review')""",
                (stable_id, revision_id),
            )
            conn.execute(
                "DELETE FROM knowledge_sources WHERE knowledge_id=?", (knowledge_id,)
            )
            source_by_id = {source["id"]: source for source in metadata["sources"]}
            claim_db_ids: dict[str, str] = {}
            for claim in metadata["claims"]:
                claim_id = f"kcl_{digest({'revision': revision_id, 'claim': claim['id']})[:32]}"
                claim_db_ids[claim["id"]] = claim_id
                conn.execute(
                    """INSERT OR IGNORE INTO professional_knowledge_claims(
                           claim_id,revision_id,local_claim_id,statement,risk_class,
                           required_validation_json) VALUES(?,?,?,?,?,?)""",
                    (
                        claim_id,
                        revision_id,
                        claim["id"],
                        claim["statement"],
                        claim["risk_class"],
                        canonical_json(claim["required_validation"]),
                    ),
                )
                for source_ref in claim["source_refs"]:
                    source = source_by_id[source_ref]
                    conn.execute(
                        """INSERT OR IGNORE INTO professional_claim_sources(
                               claim_id,source_id,source_type,stable_external_id,
                               source_version,snapshot_digest,locator_json)
                           VALUES(?,?,?,?,?,?,?)""",
                        (
                            claim_id,
                            source_ref,
                            source["type"],
                            source["stable_external_id"],
                            source["version"],
                            source["snapshot_digest"],
                            canonical_json(source["locator"]),
                        ),
                    )
                    mapping_id = f"ksm_{digest({'knowledge': knowledge_id, 'claim': claim['id'], 'source': source_ref})[:32]}"
                    conn.execute(
                        """INSERT INTO knowledge_sources(
                               mapping_id,knowledge_id,source_type,stable_external_id,url,
                               source_version,visibility,claim)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            mapping_id,
                            knowledge_id,
                            source["type"],
                            source["stable_external_id"],
                            source.get("url"),
                            source["version"],
                            source["visibility"],
                            claim["statement"],
                        ),
                    )
            for validation in metadata["validation"]:
                for local_claim_id in validation["claim_refs"]:
                    validation_id = f"kvl_{digest({'revision': revision_id, 'validation': validation['id'], 'claim': local_claim_id})[:32]}"
                    conn.execute(
                        """INSERT OR IGNORE INTO professional_validation_runs(
                               validation_id,revision_id,claim_id,layer,result,
                               environment_json,artifact_digest,case_id,observed_at)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            validation_id,
                            revision_id,
                            claim_db_ids[local_claim_id],
                            validation["layer"],
                            validation["result"],
                            canonical_json(validation["environment"]),
                            validation.get("artifact_digest"),
                            validation.get("case_id"),
                            validation["observed_at"],
                        ),
                    )
            publication_id = f"kpb_{digest({'revision': revision_id, 'bundle': bundle['bundle_digest']})[:32]}"
            conn.execute(
                """INSERT OR IGNORE INTO professional_knowledge_publications(
                       publication_id,revision_id,bundle_digest,approved_bundle_digest,
                       published_by,published_at) VALUES(?,?,?,?,?,?)""",
                (
                    publication_id,
                    revision_id,
                    bundle["bundle_digest"],
                    approved_digest,
                    reviewer_id,
                    imported_at,
                ),
            )
            imported.append(knowledge_id)
    return {**plan, "knowledge_ids": imported}
