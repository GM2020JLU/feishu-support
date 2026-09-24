from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .db import transaction
from .ids import canonical_json, digest, new_id
from .knowledge import attach_registered_source, create_candidate, register_source
from .timeutil import iso_now


class DocHistoryError(RuntimeError):
    pass


K3_TERMS = re.compile(
    r"(?i)(?:\bk3\b|spacemit|u-boot|uboot|edk2|uefi|opensbi|fastboot|brom|"
    r"串口|固件|内核|kernel|dtb|设备树|\bec\b|espi|acpi|ufs|ddr|board[0-9])"
)
_HIGHLIGHT_RE = re.compile(r"</?h[b]?>", re.IGNORECASE)


def _run_lark(args: list[str], *, timeout: int = 600) -> dict[str, Any]:
    try:
        process = subprocess.run(
            ["lark-cli", *args, "--format", "json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DocHistoryError("lark-cli request timed out") from exc
    raw = process.stdout if process.stdout.strip() else process.stderr
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DocHistoryError("lark-cli returned non-JSON output") from exc
    if process.returncode != 0 or value.get("ok") is not True:
        error = value.get("error") if isinstance(value, dict) else None
        subtype = error.get("subtype") if isinstance(error, dict) else None
        raise DocHistoryError(f"lark-cli request failed: {subtype or 'unknown'}")
    return value


def _clean_highlight(value: Any) -> str:
    return html.unescape(_HIGHLIGHT_RE.sub("", str(value or ""))).strip()


def _stable_id(result: dict[str, Any]) -> str:
    meta = result.get("result_meta") or {}
    entity_type = str(result.get("entity_type") or "unknown").lower()
    token = str(meta.get("token") or "")
    url = str(meta.get("url") or "")
    coordinate = token or url
    if not coordinate:
        coordinate = hashlib.sha256(canonical_json(result).encode()).hexdigest()
    return f"{entity_type}:{coordinate}"


def _atomic_json(path: Path, value: Any) -> str:
    encoded = canonical_json(value).encode()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".partial-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return hashlib.sha256(encoded).hexdigest()


def _search_created_documents() -> list[dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    page_token: str | None = None
    seen_tokens: set[str] = set()
    for _ in range(1000):
        args = [
            "drive", "+search", "--as", "user", "--query", "", "--created-by-me",
            "--doc-types", "doc,docx,wiki", "--sort", "create_time", "--page-size", "20",
        ]
        if page_token:
            args.extend(["--page-token", page_token])
        value = _run_lark(args)
        data = value.get("data") or {}
        page = data.get("results") or []
        for result in page:
            if isinstance(result, dict):
                results[_stable_id(result)] = result
        if not data.get("has_more"):
            return list(results.values())
        next_token = str(data.get("page_token") or "")
        if not next_token or next_token in seen_tokens:
            raise DocHistoryError("document search pagination did not advance")
        seen_tokens.add(next_token)
        page_token = next_token
    raise DocHistoryError("document search exceeded pagination safety limit")


def _fetch_document(root: Path, result: dict[str, Any]) -> dict[str, Any]:
    stable_id = _stable_id(result)
    meta = result.get("result_meta") or {}
    url = str(meta.get("url") or "")
    if not url:
        raise DocHistoryError("document result has no URL")
    value = _run_lark(
        [
            "docs", "+fetch", "--as", "user", "--doc", url,
            "--doc-format", "markdown", "--detail", "simple",
        ],
        timeout=1800,
    )
    document = (value.get("data") or {}).get("document") or {}
    content = str(document.get("content") or "")
    if not content.strip():
        raise DocHistoryError("document fetch returned empty content")
    filename = hashlib.sha256(stable_id.encode()).hexdigest() + ".json"
    archive_file = root / "documents" / filename
    content_digest = _atomic_json(
        archive_file,
        {"schema_version": 1, "search_result": result, "fetch": value},
    )
    return {
        "stable_id": stable_id,
        "archive_file": str(archive_file),
        "revision_id": str(document.get("revision_id") or ""),
        "content_chars": len(content),
        "k3_term_hits": len(K3_TERMS.findall(content)),
        "content_digest": content_digest,
    }


def _supports_doc_fetch(result: dict[str, Any]) -> bool:
    entity = str(result.get("entity_type") or "").lower()
    meta = result.get("result_meta") or {}
    doc_type = str(meta.get("doc_types") or "").lower()
    if entity in {"doc", "docx"}:
        return True
    return entity == "wiki" and doc_type in {"doc", "docx"}


def backfill(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    resume_run_id: str | None = None,
    workers: int = 4,
) -> dict[str, Any]:
    run_id = resume_run_id or new_id("dhr")
    archive_dir = data_dir / "doc-history" / run_id
    archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive_dir.chmod(0o700)
    if resume_run_id:
        row = conn.execute(
            "SELECT archive_dir FROM doc_history_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise DocHistoryError("document history run not found")
    else:
        now = iso_now()
        with transaction(conn):
            conn.execute(
                """INSERT INTO doc_history_runs(run_id,state,archive_dir,created_at,updated_at)
                   VALUES(?,'running',?,?,?)""",
                (run_id, str(archive_dir), now, now),
            )
    documents = _search_created_documents()
    with transaction(conn):
        conn.execute(
            "UPDATE doc_history_runs SET total_documents=?,state='running',updated_at=? WHERE run_id=?",
            (len(documents), iso_now(), run_id),
        )
        for result in documents:
            stable_id = _stable_id(result)
            meta = result.get("result_meta") or {}
            entity = str(result.get("entity_type") or "unknown").lower()
            doc_type = str(meta.get("doc_types") or "").lower()
            state = "pending" if _supports_doc_fetch(result) else "skipped"
            conn.execute(
                """INSERT INTO doc_history_documents(run_id,stable_id,entity_type,doc_type,title,
                       url,token,state,metadata_json,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id,stable_id) DO UPDATE SET
                     entity_type=excluded.entity_type,doc_type=excluded.doc_type,title=excluded.title,
                     url=excluded.url,token=excluded.token,metadata_json=excluded.metadata_json,
                     state=CASE WHEN doc_history_documents.state='complete' THEN 'complete' ELSE excluded.state END,
                     updated_at=excluded.updated_at""",
                (
                    run_id, stable_id, entity, doc_type,
                    _clean_highlight(result.get("title_highlighted")),
                    meta.get("url"), meta.get("token"), state,
                    canonical_json(result), iso_now(),
                ),
            )
    pending: list[dict[str, Any]] = []
    for result in documents:
        stable_id = _stable_id(result)
        row = conn.execute(
            "SELECT state FROM doc_history_documents WHERE run_id=? AND stable_id=?",
            (run_id, stable_id),
        ).fetchone()
        if row is not None and row["state"] == "pending":
            pending.append(result)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as executor:
        futures = {
            executor.submit(_fetch_document, archive_dir, result): _stable_id(result)
            for result in pending
        }
        for future in as_completed(futures):
            stable_id = futures[future]
            try:
                fetched = future.result()
            except DocHistoryError as exc:
                with transaction(conn):
                    conn.execute(
                        """UPDATE doc_history_documents SET state='failed',error_code=?,updated_at=?
                             WHERE run_id=? AND stable_id=?""",
                        (str(exc)[:160], iso_now(), run_id, stable_id),
                    )
                continue
            with transaction(conn):
                conn.execute(
                    """UPDATE doc_history_documents SET state='complete',archive_file=?,revision_id=?,
                           content_chars=?,k3_term_hits=?,content_digest=?,error_code=NULL,updated_at=?
                         WHERE run_id=? AND stable_id=?""",
                    (
                        fetched["archive_file"], fetched["revision_id"], fetched["content_chars"],
                        fetched["k3_term_hits"], fetched["content_digest"], iso_now(),
                        run_id, stable_id,
                    ),
                )
    totals = conn.execute(
        """SELECT count(*) total,sum(state='complete') complete,sum(state='skipped') skipped,
                  sum(state='failed') failed,sum(state='complete' AND k3_term_hits>0) k3
             FROM doc_history_documents WHERE run_id=?""",
        (run_id,),
    ).fetchone()
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "filter": {"original_creator": "current_user", "types": ["doc", "docx", "wiki"]},
        "counts": dict(totals),
    }
    manifest_digest = _atomic_json(archive_dir / "manifest.json", manifest)
    final_state = "complete" if int(totals["failed"] or 0) == 0 else "partial"
    with transaction(conn):
        conn.execute(
            """UPDATE doc_history_runs SET state=?,completed_documents=?,skipped_documents=?,
                   k3_documents=?,error_count=?,manifest_digest=?,updated_at=? WHERE run_id=?""",
            (
                final_state, int(totals["complete"] or 0), int(totals["skipped"] or 0),
                int(totals["k3"] or 0), int(totals["failed"] or 0), manifest_digest,
                iso_now(), run_id,
            ),
        )
    return {
        "run_id": run_id,
        "state": final_state,
        "archive_dir": str(archive_dir),
        **dict(totals),
    }


def build_review_report(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    report_path: Path,
    limit: int = 200,
) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM doc_history_runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None or run["state"] not in {"complete", "partial"}:
        raise DocHistoryError("document history run is not ready")
    rows = list(
        conn.execute(
            """SELECT * FROM doc_history_documents
                 WHERE run_id=? AND state='complete' AND k3_term_hits>0
                 ORDER BY k3_term_hits DESC,content_chars DESC LIMIT ?""",
            (run_id, max(1, min(limit, 1000))),
        )
    )
    lines = [
        "# 飞书云文档 K3 资料索引（待筛选）", "",
        (
            f"作者范围：当前飞书用户最初创建；发现 {run['total_documents']} 个资源，"
            f"读取 {run['completed_documents']}，跳过非文档节点 {run['skipped_documents']}，"
            f"K3 相关文档 {run['k3_documents']}，失败 {run['error_count']}。"
        ), "",
        "> 这只是资料发现清单，不提炼正文、不生成标准答案。Debug 笔记默认排除；只有人工加入操作指南路由表的文档才可能用于回复链接。", "",
    ]
    for number, row in enumerate(rows, 1):
        register_source(
            conn,
            source_type="feishu_doc",
            stable_external_id=str(row["stable_id"]),
            title=row["title"],
            url=row["url"],
            acl={"visibility": "private", "owner_only": True},
            source_version=row["revision_id"],
            content_digest=row["content_digest"],
            updated_at=json.loads(row["metadata_json"]).get("result_meta", {}).get("update_time_iso"),
        )
        lines.extend(
            [
                f"## {number}. {row['title'] or '(无标题)'}", "",
                f"坐标：`{row['stable_id']}`  ",
                f"版本：`{row['revision_id'] or 'unknown'}`  ",
                f"正文字符：{row['content_chars']}；K3 命中：{row['k3_term_hits']}  ",
                f"链接：{row['url']}", "",
                "路由筛选：加入操作指南白名单 / 仅作私有排查证据 / 排除", "",
            ]
        )
    report_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    report_path.chmod(0o600)
    return {"run_id": run_id, "document_count": len(rows), "report": str(report_path)}


def import_link_manifest(
    conn: sqlite3.Connection,
    *,
    manifest_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    """Import reviewed intent-to-document routes without summarizing document content."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DocHistoryError("document link manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise DocHistoryError("unsupported document link manifest schema")
    run_id = str(manifest.get("run_id") or "").strip()
    run = conn.execute(
        "SELECT state FROM doc_history_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if run is None or run["state"] not in {"complete", "partial"}:
        raise DocHistoryError("document history run is not ready")
    routes = manifest.get("routes")
    if not isinstance(routes, list) or not routes:
        raise DocHistoryError("document link manifest has no routes")

    imported: list[dict[str, Any]] = []
    for item in routes:
        if not isinstance(item, dict):
            raise DocHistoryError("document link route must be a JSON object")
        key = str(item.get("key") or "").strip()
        questions = item.get("questions")
        stable_id = str(item.get("stable_id") or "").strip()
        access_mode = str(item.get("access_mode") or "request_if_denied").strip()
        if (
            not key
            or not isinstance(questions, list)
            or not all(isinstance(value, str) and value.strip() for value in questions)
            or not stable_id
            or access_mode not in {"author_share", "request_if_denied"}
        ):
            raise DocHistoryError(f"invalid document link route: {key or 'unknown'}")
        row = conn.execute(
            """SELECT * FROM doc_history_documents
                 WHERE run_id=? AND stable_id=? AND state='complete'""",
            (run_id, stable_id),
        ).fetchone()
        if row is None or not row["title"] or not row["url"]:
            raise DocHistoryError(f"unresolved source for document link route: {key}")
        metadata = json.loads(row["metadata_json"])
        register_source(
            conn,
            source_type="feishu_doc",
            stable_external_id=stable_id,
            title=row["title"],
            url=row["url"],
            acl={
                "visibility": "internal",
                "link_only": True,
                "document_access": access_mode,
            },
            source_version=row["revision_id"],
            content_digest=row["content_digest"],
            updated_at=(metadata.get("result_meta") or {}).get("update_time_iso"),
        )
        answer = f"参考文档：[{row['title']}]({row['url']})"
        if access_mode == "request_if_denied":
            answer += "\n\n如提示无权限，请在文档页面申请访问。"
        source_digest = digest(
            {
                "kind": "feishu_document_link_route",
                "route_key": key,
                "stable_id": stable_id,
                "content_digest": row["content_digest"],
                "questions": [value.strip() for value in questions],
                "access_mode": access_mode,
            }
        )
        knowledge_id = create_candidate(
            conn,
            title=f"文档路由：{row['title']}",
            questions=[value.strip() for value in questions],
            answer_markdown=answer,
            project=str(item.get("project") or "K3").strip() or "K3",
            module=str(item.get("module") or "").strip() or None,
            software_version=str(item.get("software_version") or "").strip() or None,
            disclosure_class="internal",
            confidence=float(item.get("confidence", 0.9)),
            source_authority=float(item.get("source_authority", 0.9)),
            canonical_case_id=None,
            source_digest=source_digest,
        )
        attach_registered_source(
            conn,
            knowledge_id=knowledge_id,
            source_type="feishu_doc",
            stable_external_id=stable_id,
            claim="只回复文档标题与链接，不从正文生成标准答案。",
        )
        imported.append(
            {
                "knowledge_id": knowledge_id,
                "key": key,
                "title": row["title"],
                "url": row["url"],
                "revision_id": row["revision_id"],
                "questions": questions,
                "answer_markdown": answer,
                "access_mode": access_mode,
                "module": item.get("module"),
                "confidence": float(item.get("confidence", 0.9)),
            }
        )

    lines = [
        "# 飞书云文档操作指南路由（待审核）",
        "",
        f"来源批次：`{run_id}`；链接路由候选：{len(imported)} 条。",
        "",
        "> 所有候选均为 internal/candidate，只披露已审核的文档标题和链接，不复述正文。作者文档可直接分享；其他文档若接收者无权限，由其在页面申请。Debug 笔记不进入该路由表。",
        "",
    ]
    for number, item in enumerate(imported, 1):
        lines.extend(
            [
                f"## {number}. {item['title']}",
                "",
                f"知识 ID：`{item['knowledge_id']}`  ",
                f"版本：`{item['revision_id']}`；模块：{item['module'] or '未分类'}  ",
                f"访问模式：`{item['access_mode']}`  ",
                f"文档链接：{item['url']}  ",
                "",
                "匹配意图：",
                "",
                *[f"- {question}" for question in item["questions"]],
                "",
                "固定回复：",
                "",
                item["answer_markdown"],
                "",
                "审核结论：通过 / 修改 / 退役",
                "",
            ]
        )
    report_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    report_path.chmod(0o600)
    return {
        "run_id": run_id,
        "route_count": len(imported),
        "knowledge_ids": [item["knowledge_id"] for item in imported],
        "report": str(report_path),
    }
