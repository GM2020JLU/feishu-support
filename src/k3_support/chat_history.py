from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from .db import transaction
from .ids import canonical_json, new_id
from .knowledge import attach_registered_source, create_candidate, register_source
from .timeutil import iso_now


class ChatHistoryError(RuntimeError):
    pass


K3_TERMS = re.compile(
    r"(?i)(?:\bk3\b|spacemit|spacemit|u-boot|uboot|edk2|uefi|opensbi|fastboot|"
    r"brom|串口|固件|内核|kernel|dtb|设备树|ec\b|espi|acpi|ufs|ddr|ram启动|board[0-9])"
)
QUESTION_TERMS = re.compile(
    r"[?？]|怎么|如何|为什么|为何|请问|报错|失败|异常|问题|bug", re.IGNORECASE
)
SECRET_PATTERNS = (
    (re.compile(r"(?i)(token|secret|password|passwd|authorization)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[PHONE]"),
)


def _run_lark(args: list[str]) -> dict[str, Any]:
    process = subprocess.run(
        ["lark-cli", *args, "--format", "json"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    try:
        value = json.loads(process.stdout or process.stderr)
    except json.JSONDecodeError as exc:
        raise ChatHistoryError("lark-cli returned non-JSON output") from exc
    if process.returncode != 0 or value.get("ok") is not True:
        error = value.get("error") if isinstance(value, dict) else None
        subtype = error.get("subtype") if isinstance(error, dict) else None
        raise ChatHistoryError(f"lark-cli request failed: {subtype or 'unknown'}")
    pagination = (value.get("meta") or {}).get("pagination") or {}
    if pagination and pagination.get("complete") is False:
        raise ChatHistoryError("lark-cli pagination limit was reached")
    return value


def _items(value: dict[str, Any], *names: str) -> list[dict[str, Any]]:
    data = value.get("data") or {}
    for name in names:
        found = data.get(name) if isinstance(data, dict) else None
        if isinstance(found, list):
            return [item for item in found if isinstance(item, dict)]
    return []


def _message_text(message: dict[str, Any]) -> str:
    body = message.get("body") or {}
    content = body.get("content") if isinstance(body, dict) else None
    if content is None:
        content = message.get("content")
    if isinstance(content, dict):
        content = canonical_json(content)
    if not isinstance(content, str):
        return ""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content
    pieces: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"text", "title", "content"} and isinstance(item, str):
                    pieces.append(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(parsed)
    return "\n".join(piece.strip() for piece in pieces if piece.strip())


def _redact(text: str) -> str:
    value = text
    for pattern, replacement in SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value.strip()


def _message_epoch(message: dict[str, Any]) -> float | None:
    raw = message.get("create_time")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    while value > 10_000_000_000:
        value /= 1000
    return value


def _atomic_json(path: Path, value: Any) -> str:
    encoded = canonical_json(value).encode()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".partial-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(encoded).hexdigest()


def _chat_file(root: Path, chat_id: str) -> Path:
    return root / "chats" / f"{hashlib.sha256(chat_id.encode()).hexdigest()}.json"


def _reindex_completed(conn: sqlite3.Connection, run_id: str) -> None:
    """Rebuild derived counts from immutable raw archives after parser upgrades."""
    for row in conn.execute(
        """SELECT chat_id,archive_file FROM chat_history_chats
             WHERE run_id=? AND state='complete' AND archive_file IS NOT NULL""",
        (run_id,),
    ):
        try:
            value = json.loads(Path(row["archive_file"]).read_text(encoding="utf-8"))
            messages = value.get("messages") or []
            matched = sum(bool(K3_TERMS.search(_message_text(message))) for message in messages)
        except (OSError, ValueError, TypeError):
            continue
        with transaction(conn):
            conn.execute(
                """UPDATE chat_history_chats SET message_count=?,matched_message_count=?,updated_at=?
                     WHERE run_id=? AND chat_id=?""",
                (len(messages), matched, iso_now(), run_id, row["chat_id"]),
            )


def _fetch_chat(
    archive_dir: Path,
    chat: dict[str, Any],
    *,
    start_at: str,
    end_at: str,
) -> dict[str, Any]:
    chat_id = str(chat["chat_id"])
    value = _run_lark(
        [
            "im", "+chat-messages-list", "--as", "user", "--chat-id", chat_id,
            "--start", start_at, "--end", end_at, "--order", "asc",
            "--page-all", "--page-limit", "1000", "--page-size", "50",
            "--page-delay", "100", "--no-reactions",
        ]
    )
    messages = _items(value, "messages", "items")
    thread_ids = {
        str(message.get("thread_id"))
        for message in messages
        if str(message.get("thread_id") or "").startswith("omt_")
    }
    by_id = {str(message.get("message_id")): message for message in messages}
    for thread_id in sorted(thread_ids):
        thread = _run_lark(
            [
                "im", "+threads-messages-list", "--as", "user", "--thread", thread_id,
                "--order", "asc", "--page-all", "--page-limit", "1000",
                "--page-size", "50", "--page-delay", "100", "--no-reactions",
            ]
        )
        for message in _items(thread, "messages", "items"):
            by_id[str(message.get("message_id"))] = message
    messages = sorted(by_id.values(), key=lambda item: str(item.get("create_time") or ""))
    matched = sum(bool(K3_TERMS.search(_message_text(message))) for message in messages)
    path = _chat_file(archive_dir, chat_id)
    content_digest = _atomic_json(
        path,
        {"schema_version": 1, "chat": chat, "messages": messages},
    )
    return {
        "chat_id": chat_id,
        "archive_file": str(path),
        "message_count": len(messages),
        "matched_message_count": matched,
        "content_digest": content_digest,
    }


def backfill(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    start_at: str,
    end_at: str,
    resume_run_id: str | None = None,
    workers: int = 4,
) -> dict[str, Any]:
    datetime.fromisoformat(start_at)
    datetime.fromisoformat(end_at)
    now = iso_now()
    run_id = resume_run_id or new_id("chr")
    archive_dir = data_dir / "chat-history" / run_id
    archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive_dir.chmod(0o700)
    if resume_run_id:
        row = conn.execute(
            "SELECT start_at,end_at,archive_dir FROM chat_history_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None or row["start_at"] != start_at or row["end_at"] != end_at:
            raise ChatHistoryError("resume range does not match the existing run")
    else:
        with transaction(conn):
            conn.execute(
                """INSERT INTO chat_history_runs(run_id,start_at,end_at,state,archive_dir,created_at,updated_at)
                   VALUES(?,?,?,'running',?,?,?)""",
                (run_id, start_at, end_at, str(archive_dir), now, now),
            )
    chats_value = _run_lark(
        [
            "im", "+chat-list", "--as", "user", "--types", "p2p,group",
            "--sort", "active_time", "--page-all", "--page-limit", "1000",
            "--page-size", "100", "--page-delay", "100",
        ]
    )
    chats = _items(chats_value, "chats", "items")
    _reindex_completed(conn, run_id)
    with transaction(conn):
        conn.execute(
            "UPDATE chat_history_runs SET total_chats=?,state='running',updated_at=? WHERE run_id=?",
            (len(chats), iso_now(), run_id),
        )
        for chat in chats:
            chat_id = str(chat.get("chat_id") or "")
            if chat_id:
                conn.execute(
                    """INSERT OR IGNORE INTO chat_history_chats(run_id,chat_id,state,updated_at)
                       VALUES(?,?,'pending',?)""",
                    (run_id, chat_id, iso_now()),
                )
    pending: list[dict[str, Any]] = []
    for chat in chats:
        chat_id = str(chat.get("chat_id") or "")
        if not chat_id:
            continue
        state = conn.execute(
            "SELECT state FROM chat_history_chats WHERE run_id=? AND chat_id=?",
            (run_id, chat_id),
        ).fetchone()
        if state is not None and state["state"] == "complete":
            continue
        pending.append(chat)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as executor:
        futures = {
            executor.submit(
                _fetch_chat,
                archive_dir,
                chat,
                start_at=start_at,
                end_at=end_at,
            ): str(chat["chat_id"])
            for chat in pending
        }
        for future in as_completed(futures):
            chat_id = futures[future]
            try:
                result = future.result()
            except ChatHistoryError as exc:
                with transaction(conn):
                    conn.execute(
                        """UPDATE chat_history_chats SET state='failed',error_code=?,updated_at=?
                             WHERE run_id=? AND chat_id=?""",
                        (str(exc)[:160], iso_now(), run_id, chat_id),
                    )
                continue
            with transaction(conn):
                conn.execute(
                    """UPDATE chat_history_chats SET state='complete',archive_file=?,message_count=?,
                           matched_message_count=?,content_digest=?,error_code=NULL,updated_at=?
                         WHERE run_id=? AND chat_id=?""",
                    (
                        result["archive_file"], result["message_count"],
                        result["matched_message_count"], result["content_digest"],
                        iso_now(), run_id, chat_id,
                    ),
                )
    totals = conn.execute(
        """SELECT count(*) total,sum(state='complete') complete,
                  coalesce(sum(message_count),0) messages,
                  coalesce(sum(matched_message_count),0) matched,
                  sum(state='failed') failed
             FROM chat_history_chats WHERE run_id=?""",
        (run_id,),
    ).fetchone()
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "range": {"start": start_at, "end": end_at},
        "counts": dict(totals),
    }
    manifest_digest = _atomic_json(archive_dir / "manifest.json", manifest)
    final_state = "complete" if int(totals["failed"] or 0) == 0 else "partial"
    with transaction(conn):
        conn.execute(
            """UPDATE chat_history_runs SET state=?,completed_chats=?,message_count=?,
                   matched_message_count=?,error_count=?,manifest_digest=?,updated_at=? WHERE run_id=?""",
            (
                final_state, int(totals["complete"] or 0), int(totals["messages"] or 0),
                int(totals["matched"] or 0), int(totals["failed"] or 0),
                manifest_digest, iso_now(), run_id,
            ),
        )
    return {"run_id": run_id, "state": final_state, "archive_dir": str(archive_dir), **dict(totals)}


def extract_candidates(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    report_path: Path,
    owner_open_id: str,
    limit: int = 250,
) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM chat_history_runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None or run["state"] not in {"complete", "partial"}:
        raise ChatHistoryError("history run is not ready for extraction")
    candidates: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    for row in conn.execute(
        """SELECT chat_id,archive_file FROM chat_history_chats
             WHERE run_id=? AND state='complete' AND matched_message_count>0 ORDER BY chat_id""",
        (run_id,),
    ):
        value = json.loads(Path(row["archive_file"]).read_text(encoding="utf-8"))
        messages = value.get("messages") or []
        for index, message in enumerate(messages):
            question = _redact(_message_text(message))
            if not question or not K3_TERMS.search(question) or not QUESTION_TERMS.search(question):
                continue
            sender = message.get("sender") or {}
            sender_id = str(sender.get("id") or sender.get("sender_id") or "")
            if sender_id == owner_open_id:
                continue
            answers: list[str] = []
            source_ids = [str(message.get("message_id") or "")]
            question_epoch = _message_epoch(message)
            for following in messages[index + 1 : index + 9]:
                following_epoch = _message_epoch(following)
                if (
                    question_epoch is not None
                    and following_epoch is not None
                    and following_epoch - question_epoch > 2 * 60 * 60
                ):
                    break
                following_sender = following.get("sender") or {}
                following_id = str(following_sender.get("id") or following_sender.get("sender_id") or "")
                text = _redact(_message_text(following))
                if following_id != owner_open_id and text and QUESTION_TERMS.search(text):
                    break
                if following_id == owner_open_id and text:
                    answers.append(text)
                    source_ids.append(str(following.get("message_id") or ""))
                    if len(answers) >= 3:
                        break
            if not answers:
                continue
            normalized = re.sub(r"\W+", "", question.lower())[:160]
            if normalized in seen_questions:
                continue
            seen_questions.add(normalized)
            candidates.append(
                {
                    "chat_id": row["chat_id"],
                    "question": question[:800],
                    "answer": "\n\n".join(answers)[:2400],
                    "source_ids": [item for item in source_ids if item],
                }
            )
            if len(candidates) >= limit:
                break
        if len(candidates) >= limit:
            break
    created: list[dict[str, str]] = []
    for item in candidates:
        source_digest = hashlib.sha256(canonical_json(item["source_ids"]).encode()).hexdigest()
        title = re.sub(r"\s+", " ", item["question"]).strip()[:80]
        knowledge_id = create_candidate(
            conn,
            title=title,
            questions=[item["question"]],
            answer_markdown=item["answer"],
            project="K3",
            module=None,
            software_version=None,
            disclosure_class="private",
            confidence=0.45,
            source_authority=0.55,
            canonical_case_id=None,
            source_digest=source_digest,
        )
        for message_id in item["source_ids"]:
            stable_id = f"chat:{item['chat_id']}:message:{message_id}"
            register_source(
                conn,
                source_type="feishu_message",
                stable_external_id=stable_id,
                title="Feishu chat evidence",
                url=None,
                acl={"visibility": "private", "owner_only": True},
                source_version=message_id,
                content_digest=None,
                updated_at=None,
            )
            attach_registered_source(
                conn,
                knowledge_id=knowledge_id,
                source_type="feishu_message",
                stable_external_id=stable_id,
                claim="Historical question or owner answer; requires owner review",
            )
        created.append({"knowledge_id": knowledge_id, "title": title})
    lines = [
        "# K3 聊天知识候选（待审核）",
        "",
        f"归档范围：{run['start_at']} — {run['end_at']}",
        f"归档消息：{run['message_count']}；K3 关键词命中：{run['matched_message_count']}；候选：{len(created)}",
        "",
        "> 所有条目均为 private/candidate，未审核前不会用于自动回复。敏感格式已做基础脱敏，但仍需人工确认技术结论、版本范围和披露级别。",
        "",
    ]
    for number, item in enumerate(created, 1):
        row = conn.execute(
            "SELECT question_variants_json,answer_markdown FROM knowledge_entries WHERE knowledge_id=?",
            (item["knowledge_id"],),
        ).fetchone()
        question = json.loads(row["question_variants_json"])[0]
        lines.extend(
            [
                f"## {number}. {item['title']}", "", f"ID: `{item['knowledge_id']}`", "",
                "问题：", "", question, "", "历史回复：", "", row["answer_markdown"], "",
                "审核建议：确认 / 修改 / 合并 / 退回", "",
            ]
        )
    report_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    report_path.chmod(0o600)
    return {"run_id": run_id, "candidate_count": len(created), "report": str(report_path)}
