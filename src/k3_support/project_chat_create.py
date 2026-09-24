"""Authenticated chat commands over scoped Project creation controls.

The chat adapter never holds Project credentials. Creation dispatch uses the
existing control layer's grant, duplicate, native transport and result gates.
"""

import json
from datetime import timedelta

from . import project_bug_create, project_create_grants
from .db import atomic
from .ids import canonical_json, digest
from .timeutil import iso_now, parse_iso, utc_now

COMMANDS = {
    "create-scope-options", "issue-create-grant", "create-options", "create-draft", "create-note",
    "create-grants", "revoke-create-grant", "draft", "drafts",
    "create-search", "create-attach", "create-confirm", "create-ready",
    "create-dispatch", "create-bind",
}
MAX_MESSAGE_UNITS = 3000
MAX_JSON_BYTES = 256_000
MAX_NOTE_BYTES = 12_000
MAX_NOTE_TITLE_CHARS = 300


def _error(message):
    from .project_bugs import BugConflict

    raise BugConflict(message)


def _units(text):
    return len(text.encode("utf-16-le")) // 2


def _result(text):
    if _units(text) > MAX_MESSAGE_UNITS:
        return {
            "command": "project_bug",
            "text": "内容过长，完整内容未截断且没有准备可执行操作；请在网页查看。",
        }
    return {"command": "project_bug", "text": text}


def _json_text(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    for separator in ("\u0085", "\u2028", "\u2029"):
        text = text.replace(separator, "\\u" + format(ord(separator), "04x"))
    return text


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("non-finite JSON number")


def _field_values(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("field values JSON is too large")
    try:
        value = json.loads(text, object_pairs_hook=_object_pairs, parse_constant=_invalid_constant)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ValueError("field values must be valid JSON without duplicate keys or NaN") from None
    if not isinstance(value, dict) or not value:
        raise ValueError("field values JSON must be a nonempty object")
    # Match the persistent draft's serialization boundary before any remote read.
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("field values must be finite JSON data") from None
    if len(encoded) > MAX_JSON_BYTES:
        raise ValueError("field values JSON is too large")
    return value


def _grant(conn, actor, grant_id, *, require_active=True):
    if not isinstance(grant_id, str) or not grant_id.strip() or len(grant_id) > 256:
        raise ValueError("invalid create grant ID")
    row = conn.execute("SELECT * FROM project_create_grants WHERE grant_id=?", (grant_id,)).fetchone()
    if row is None or row["actor"] != actor:
        raise PermissionError("create grant is not owned by this operator")
    scope = {key: row[key] for key in ("host", "project_key", "type_key")}
    if require_active and not project_create_grants.covers(
        conn, grant_id=grant_id, actor=actor, **scope
    ):
        raise PermissionError("create grant is expired, revoked, exhausted, or out of scope")
    return row, scope


def _decode_stored_values(row):
    try:
        value = json.loads(row["field_values_json"], object_pairs_hook=_object_pairs,
                           parse_constant=_invalid_constant)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("stored create draft is unavailable") from None
    if not isinstance(value, dict):
        raise TypeError("stored create draft is unavailable")
    return value


def _matches_existing(row, *, actor, grant_id, field_values):
    if row["actor"] != actor or row["grant_id"] != grant_id:
        return False
    stored = _decode_stored_values(row)
    return canonical_json(stored) == canonical_json(field_values)


def _existing(conn, *, actor, request_id, grant_id, field_values):
    rows = conn.execute(
        "SELECT * FROM project_bug_create_drafts WHERE request_id=? ORDER BY rowid LIMIT 3",
        (request_id,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1 or rows[0]["actor"] != actor:
        _error("native message ID is already bound to another create draft")
    row = rows[0]
    if not _matches_existing(row, actor=actor, grant_id=grant_id, field_values=field_values):
        _error("native message ID is already bound to a different create draft intent")
    grant = conn.execute("SELECT * FROM project_create_grants WHERE grant_id=?", (row["grant_id"],)).fetchone()
    if (grant is None or grant["actor"] != actor
            or any(grant[key] != row[key] for key in ("host", "project_key", "type_key"))):
        _error("saved create draft grant binding is unavailable")
    return row


def _parse_create_draft(argv):
    if not isinstance(argv, list) or len(argv) != 4 or argv[:2] != ["bug", "create-draft"]:
        return None
    try:
        return argv[2], _field_values(argv[3])
    except (ValueError, UnicodeError):
        return None


def _note_values(text):
    """Map one chat note to the two official native text fields."""
    if not isinstance(text, str):
        raise TypeError("note text must be text")
    try:
        encoded = text.encode("utf-8")
    except UnicodeError:
        raise ValueError("note text must be valid UTF-8") from None
    if not encoded or len(encoded) > MAX_NOTE_BYTES:
        raise ValueError("note text must be 1–12000 UTF-8 bytes")
    title = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not title or len(title) > MAX_NOTE_TITLE_CHARS or "\x00" in title:
        raise ValueError("note title must be a nonblank line of at most 300 characters")
    if "\x00" in text:
        raise ValueError("note text contains an invalid NUL character")
    return {"name": title, "description": text}


def _parse_create_note(argv):
    if not isinstance(argv, list) or len(argv) != 4 or argv[:2] != ["bug", "create-note"]:
        return None
    try:
        return argv[2], _note_values(argv[3])
    except (ValueError, UnicodeError):
        return None


def _note_fields(fields):
    by_key = {field["field_key"]: field for field in fields}
    missing = [key for key in ("name", "description") if key not in by_key]
    if missing:
        raise ValueError("official Project creation form lacks note fields: " + ", ".join(missing))
    invalid = [key for key in ("name", "description")
               if (by_key[key].get("editor") != "text"
                   or by_key[key].get("type") not in {"text", "multi_text", "multi-text"})]
    if invalid:
        raise ValueError("official Project note fields are not editable text: " + ", ".join(invalid))


def _reserve_note_intent(conn, *, actor, request_id, argv, draft_id):
    """Bind a note's exact native command to the draft in the same transaction."""
    signature = digest({"argv": argv})
    conn.execute(
        "INSERT OR IGNORE INTO project_create_chat_intents "
        "(request_id,actor,draft_id,intent_digest,created_at) VALUES(?,?,?,?,?)",
        (request_id, actor, draft_id, signature, iso_now()),
    )
    row = conn.execute(
        "SELECT actor,draft_id,intent_digest FROM project_create_chat_intents WHERE request_id=?",
        (request_id,),
    ).fetchone()
    if (row is None or row["actor"] != actor or row["draft_id"] != draft_id
            or row["intent_digest"] != signature):
        _error("native message ID is already bound to a different create intent")


def _mutation_draft_id(argv):
    if (not isinstance(argv, list) or len(argv) not in {4, 5}
            or len(argv) < 3 or argv[1] not in {
                "create-search", "create-attach", "create-confirm", "create-ready",
                "create-dispatch", "create-bind",
            }):
        return None
    return argv[2]


def _reserve_mutation_intent(conn, actor, request_id, argv):
    """Make native-message replays immutable before any draft mutation."""
    draft_id = _mutation_draft_id(argv)
    if draft_id is None:
        return False
    intent_digest = digest({"argv": argv})
    conn.execute(
        "INSERT OR IGNORE INTO project_create_chat_intents "
        "(request_id,actor,draft_id,intent_digest,created_at) VALUES(?,?,?,?,?)",
        (request_id, actor, draft_id, intent_digest, iso_now()),
    )
    row = conn.execute(
        "SELECT actor,draft_id,intent_digest FROM project_create_chat_intents WHERE request_id=?",
        (request_id,),
    ).fetchone()
    if (row is None or row["actor"] != actor or row["draft_id"] != draft_id
            or row["intent_digest"] != intent_digest):
        _error("native message ID is already bound to a different create intent")
    return True


def check_intent(conn, config, argv, request_id):
    """Fence a previously prepared draft if the same native message is edited."""
    actor = config.control_operator_id
    if not actor:
        raise PermissionError("Project creation requires a configured operator")
    issued = conn.execute(
        "SELECT * FROM project_create_grants WHERE request_id=?", (request_id,)
    ).fetchone()
    if issued is not None:
        if (issued["actor"] != actor or len(argv) != 6 or argv[:2] != ["bug", "issue-create-grant"]
                or argv[2] != issued["project_key"] or argv[3] != issued["type_key"]
                or argv[4] != str(issued["max_creations"])
                or argv[5] != issued["expires_at"]):
            _error("native message ID is already bound to a different create grant intent")
        return True
    revoked = conn.execute(
        "SELECT actor,grant_id,intent_digest FROM project_create_chat_grant_revocations WHERE request_id=?",
        (request_id,),
    ).fetchone()
    if revoked is not None:
        if (revoked["actor"] != actor or len(argv) != 3
                or argv[:2] != ["bug", "revoke-create-grant"]
                or argv[2] != revoked["grant_id"]
                or revoked["intent_digest"] != digest({"argv": argv})):
            _error("native message ID is already bound to a different create grant revocation")
        return True
    intent = conn.execute(
        "SELECT actor,draft_id,intent_digest FROM project_create_chat_intents WHERE request_id=?",
        (request_id,),
    ).fetchone()
    if intent is not None:
        draft_id = _mutation_draft_id(argv)
        note = _parse_create_note(argv)
        if (intent["actor"] != actor or (draft_id != intent["draft_id"] and note is None)
                or intent["intent_digest"] != digest({"argv": argv})):
            _error("native message ID is already bound to a different create intent")
        return True
    rows = conn.execute(
        "SELECT * FROM project_bug_create_drafts WHERE request_id=? ORDER BY rowid LIMIT 3",
        (request_id,),
    ).fetchall()
    if not rows:
        if len(argv) == 3 and argv[:2] == ["bug", "revoke-create-grant"]:
            _grant(conn, actor, argv[2], require_active=False)
            signature = digest({"argv": argv})
            conn.execute(
                "INSERT OR IGNORE INTO project_create_chat_grant_revocations "
                "(request_id,actor,grant_id,intent_digest,created_at) VALUES(?,?,?,?,?)",
                (request_id, actor, argv[2], signature, iso_now()),
            )
            row = conn.execute(
                "SELECT actor,grant_id,intent_digest FROM project_create_chat_grant_revocations WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if (row is None or row["actor"] != actor or row["grant_id"] != argv[2]
                    or row["intent_digest"] != signature):
                _error("native message ID is already bound to a different create grant revocation")
            return True
        return _reserve_mutation_intent(conn, actor, request_id, argv)
    if len(rows) != 1 or rows[0]["actor"] != actor:
        _error("native message ID is already bound to a different create intent")
    parsed = _parse_create_draft(argv)
    if parsed is None:
        _error("native message ID is already bound to a different create intent")
    grant_id, values = parsed
    if not _matches_existing(rows[0], actor=actor, grant_id=grant_id, field_values=values):
        _error("native message ID is already bound to a different create intent")
    grant = conn.execute("SELECT * FROM project_create_grants WHERE grant_id=?", (grant_id,)).fetchone()
    if (grant is None or grant["actor"] != actor
            or any(grant[key] != rows[0][key] for key in ("host", "project_key", "type_key"))):
        _error("native message ID is already bound to a different create grant")
    return True


def _form(conn, config, actor, grant_id):
    _row, scope = _grant(conn, actor, grant_id)
    from .project_bug_controls import execute

    result = execute(conn, config, action="create-form", payload={"grant_id": grant_id, **scope})
    fields = result.get("fields") if isinstance(result, dict) else None
    if not isinstance(fields, list) or not fields or len(fields) > 200:
        raise ValueError("official Project creation form is unavailable")
    seen = set()
    required = []
    for field in fields:
        if (not isinstance(field, dict) or not isinstance(field.get("field_key"), str)
                or field["field_key"] in seen or type(field.get("required")) is not bool
                or not isinstance(field.get("label"), str)):
            raise ValueError("official Project creation form is invalid")
        seen.add(field["field_key"])
        if field["required"]:
            required.append({"field_key": field["field_key"], "label": field["label"]})
    return scope, fields, required


def _options_text(scope, fields):
    lines = ["Project 新建表单（官方只读元数据；不自动填默认值）",
             f"目标：{scope['host']} / {scope['project_key']} / {scope['type_key']}"]
    for field in fields:
        lines.append(f"{field['field_key']} · {field['label']} · {'必填' if field['required'] else '选填'} · {field.get('type', '类型未知')}")
        options = field.get("options")
        if options is not None:
            lines.append("建议值（官方当前选项）：" + _json_text(options))
    lines.append("创建草稿：bug create-draft <grant_id> '<field_values JSON>'")
    lines.append("这里只保存本地草稿；不会创建远端缺陷。")
    return "\n".join(lines)


def _draft_text(row):
    try:
        value = row if isinstance(row, dict) and "field_values" in row else project_bug_create.projection(row)
    except (KeyError, IndexError, TypeError):
        raise ValueError("stored create draft is unavailable") from None
    preview = {
        "draft_id": value["draft_id"], "state": value["state"],
        "scope": value["scope"], "field_values": value["field_values"],
        "required_fields": value["required_fields"], "missing_required": value["missing_required"],
        "duplicate_search_id": value["duplicate_search_id"],
        "duplicate_candidates": value["duplicate_candidates"],
        "duplicate_confirmed": value["duplicate_confirmed"],
        "created_item_id": value["created_item_id"], "error_code": value["error_code"],
        "request_digest": value["request_digest"],
    }
    return "完整本地草稿与结果记录（远端结果仍需回读核对）：\n" + _json_text(preview)


def _draft_result(row, prefix=""):
    text = prefix + _draft_text(row)
    next_step = {
        "created": "下一步：在网页回读并绑定已创建对象，不要重复创建。",
        "unknown": "下一步：在网页核对原创建请求的远端结果，不要重发。",
        "dispatched": "下一步：在网页核对原创建请求的结果，不要重发。",
        "cancelled": "该草稿已取消，保留历史记录。",
        "rejected": "该草稿已被拒绝，请在网页查看原因。",
    }.get(row["state"], "下一步：在网页读取创建草稿，补充必填项、查重并确认创建。")
    text += "\n" + next_step
    if _units(text) > MAX_MESSAGE_UNITS:
        return _result(f"本地草稿：{row['draft_id']}\n状态：{row['state']}\n完整预览超过聊天显示上限，请在网页读取此草稿。\n{next_step}")
    return _result(text)


def _owned_with_digest(conn, actor, draft_id, expected_digest):
    row = project_bug_create._owned(conn, draft_id, actor)
    project_bug_create._expect(row, expected_digest)
    return row


def _creation_help():
    return (
        "用法：bug create-scope-options | issue-create-grant <项目Key> <类型Key> <上限> <到期ISO> | "
        "create-options <授权ID> | create-draft <授权ID> '<字段 JSON>' | "
        "create-note <授权ID> '<首个非空行作为标题，全文作为描述>' | "
        "create-search <草稿ID> <请求摘要> <关键词> | "
        "create-attach <草稿ID> <请求摘要> <搜索ID> | "
        "create-confirm <草稿ID> <请求摘要> | "
        "create-ready <草稿ID> <请求摘要> | "
        "create-dispatch <草稿ID> <请求摘要> | "
        "create-bind <草稿ID> <请求摘要> | "
        "draft <草稿ID> | drafts [游标] | create-grants [游标] | "
        "revoke-create-grant <授权ID>"
    )


def _drafts_text(items, cursor):
    lines = ["本地 Project 创建草稿"]
    visible = items[:8]
    for item in visible:
        lines.append(f"{item['draft_id']} · {item['state']} · {item['scope']['project_key']}/{item['scope']['type_key']}")
        if item["missing_required"]:
            lines.append("缺少必填：" + ", ".join(item["missing_required"]))
        if item.get("created_item_id"):
            lines.append("远端条目 ID：" + item["created_item_id"])
        if item.get("duplicate_candidates"):
            lines.append("疑似重复：" + _json_text(item["duplicate_candidates"]))
    if len(items) > 8:
        cursor = visible[-1]["draft_id"]
    if cursor:
        lines.append("下一页：bug drafts " + cursor)
    if not visible:
        lines.append("暂无本地草稿。")
    return "\n".join(lines)


def _grants_text(items, cursor):
    lines = ["当前操作者的 Project 创建授权（不会自动签发）"]
    for item in items[:8]:
        scope = item["scope"]
        lines.append(f"{item['grant_id']} · {item['status']} · {scope['host']} / {scope['project_key']} / {scope['type_key']}")
        lines.append(f"有效期至：{item['expires_at']} · 剩余次数：{item['remaining']}/{scope['max_creations']}")
    if len(items) > 8:
        cursor = items[7]["grant_id"]
    if cursor:
        lines.append("下一页：bug create-grants " + cursor)
    if not items:
        lines.append("暂无授权；需由受信任控制入口明确签发。")
    return "\n".join(lines)


def route(conn, config, argv, request_id):
    """Handle authenticated ``bug create-*`` chat commands."""
    from .control import ControlError
    from .project_bug_controls import execute
    from .project_bugs import BugConflict

    actor = config.control_operator_id
    if not actor:
        raise PermissionError("Project creation requires a configured operator")
    if argv == ["bug", "create-scope-options"]:
        value = execute(conn, config, action="create-scope-options", payload={})
        if not value["available"]:
            return _result("当前没有已配置的飞书项目创建范围；不会签发创建授权。")
        expiry = (utc_now() + timedelta(hours=8)).isoformat()
        lines = ["已配置的飞书项目创建范围（签发授权不会创建缺陷）："]
        for item in value["options"][:8]:
            lines.append(f"{item['simple_name']} · {item['project_key']} / {item['type_key']}")
            lines.append(f"bug issue-create-grant {item['project_key']} {item['type_key']} 1 {expiry}")
        if len(value["options"]) > 8:
            lines.append("其余配置范围请在网页查看；聊天命令仍须使用准确项目与类型 ID。")
        lines.append("示例到期时间为本次读取后 8 小时；可选择更早时间，最长不超过 24 小时。")
        return _result("\n".join(lines))
    if len(argv) == 6 and argv[1] == "issue-create-grant":
        old = conn.execute(
            "SELECT * FROM project_create_grants WHERE actor=? AND request_id=?",
            (actor, request_id),
        ).fetchone()
        if old is not None:
            result = project_create_grants.projection(old)
        else:
            if not argv[4].isdigit() or not 1 <= int(argv[4]) <= 20:
                raise ValueError("最大创建数须为 1–20")
            expiry = parse_iso(argv[5])
            now = utc_now()
            if expiry <= now or expiry > now + timedelta(hours=24):
                raise ValueError("创建授权到期时间须在未来 24 小时内")
            available = execute(conn, config, action="create-scope-options", payload={})
            if (not available["available"] or not any(
                    item["project_key"] == argv[2] and item["type_key"] == argv[3]
                    for item in available["options"])):
                raise PermissionError("项目和缺陷类型不在已配置的创建范围")
            result = execute(conn, config, action="issue-create-grant", payload={
                "request_id": request_id,
                "scope": {"host": available["reader_host"], "project_key": argv[2],
                          "type_key": argv[3], "max_creations": int(argv[4])},
                "expires_at": argv[5],
            })
        return _result(f"创建授权：{result['grant_id']} · {result['status']} · "
                       f"{result['scope']['project_key']} / {result['scope']['type_key']} · "
                       f"上限 {result['scope']['max_creations']} · 到期 {result['expires_at']}\n"
                       f"查看表单：bug create-options {result['grant_id']}\n"
                       "此授权不会直接创建缺陷；必填、查重、身份和远端写入门槛仍须通过。")
    if len(argv) == 3 and argv[1] == "create-options":
        scope, fields, _required = _form(conn, config, actor, argv[2])
        return _result(_options_text(scope, fields))
    if len(argv) == 4 and argv[1] == "create-draft":
        grant_id, values = argv[2], _field_values(argv[3])
        existing = _existing(conn, actor=actor, request_id=request_id,
                             grant_id=grant_id, field_values=values)
        if existing is not None:
            return _draft_result(existing, "已存在相同请求的本地草稿：\n")
        scope, fields, required = _form(conn, config, actor, grant_id)
        allowed = {field["field_key"] for field in fields}
        extras = sorted(set(values) - allowed)
        if extras:
            raise ValueError("字段不在当前官方新建表单中：" + ", ".join(extras))
        try:
            created = execute(conn, config, action="prepare-create-draft", payload={
                "request_id": request_id, "grant_id": grant_id, **scope,
                "field_values": values, "required_fields": required,
            })
        except BugConflict:
            # Concurrent delivery may have committed while metadata was read.
            # Reuse only the exact original fields and grant, never changed intent.
            created = _existing(conn, actor=actor, request_id=request_id,
                                grant_id=grant_id, field_values=values)
            if created is None:
                raise
        return _draft_result(created, "本地创建草稿已记录：\n")
    if len(argv) == 4 and argv[1] == "create-note":
        grant_id, values = argv[2], _note_values(argv[3])
        existing = _existing(conn, actor=actor, request_id=request_id,
                             grant_id=grant_id, field_values=values)
        if existing is not None:
            return _draft_result(existing, "已存在相同请求的本地草稿：\n")
        scope, fields, required = _form(conn, config, actor, grant_id)
        _note_fields(fields)
        with atomic(conn):
            try:
                created = execute(conn, config, action="prepare-create-draft", payload={
                    "request_id": request_id, "grant_id": grant_id, **scope,
                    "field_values": values, "required_fields": required,
                })
            except BugConflict:
                created = _existing(conn, actor=actor, request_id=request_id,
                                     grant_id=grant_id, field_values=values)
                if created is None:
                    raise
            _reserve_note_intent(conn, actor=actor, request_id=request_id,
                                 argv=argv, draft_id=created["draft_id"])
        return _draft_result(created, "本地笔记草稿已记录：\n")
    if len(argv) in {2, 3} and argv[1] == "draft":
        if len(argv) != 3:
            raise ControlError("usage: bug draft <draft_id>")
        row = project_bug_create._owned(conn, argv[2], actor)
        return _draft_result(row)
    if len(argv) in {2, 3} and argv[1] == "drafts":
        cursor = argv[2] if len(argv) == 3 else ""
        result = execute(conn, config, action="create-drafts", payload={"after_id": cursor})
        return _result(_drafts_text(result["items"], result["next_cursor"]))
    if len(argv) in {2, 3} and argv[1] == "create-grants":
        cursor = argv[2] if len(argv) == 3 else ""
        result = execute(conn, config, action="list-create-grants", payload={"after_id": cursor})
        return _result(_grants_text(result["items"], result["next_cursor"]))
    if len(argv) == 3 and argv[1] == "revoke-create-grant":
        _grant(conn, actor, argv[2], require_active=False)
        result = execute(conn, config, action="revoke-create-grant", payload={"grant_id": argv[2]})
        return _result(f"创建授权已吊销：{result['grant_id']}。已保存的草稿保留，但不能使用该授权派发创建。")
    if len(argv) == 5 and argv[1] == "create-search":
        draft_id, expected_digest, keyword = argv[2:]
        _owned_with_digest(conn, actor, draft_id, expected_digest)
        result = execute(conn, config, action="search-create-duplicates", payload={
            "draft_id": draft_id, "expected_digest": expected_digest,
            "keyword": keyword, "request_id": request_id,
        })
        return _result(
            f"重复缺陷搜索已记录：{result['search_id']} · {result['state']}\n"
            f"搜索完成后附加结果：bug create-attach {draft_id} {expected_digest} {result['search_id']}"
        )
    if len(argv) == 5 and argv[1] == "create-attach":
        draft_id, expected_digest, search_id = argv[2:]
        row = _owned_with_digest(conn, actor, draft_id, expected_digest)
        # A native redelivery after confirmation must not clear that confirmation.
        if row["duplicate_search_id"] == search_id:
            return _draft_result(row, "相同重复搜索已附加，未重置草稿：\n")
        result = execute(conn, config, action="attach-create-search", payload={
            "draft_id": draft_id, "expected_digest": expected_digest, "search_id": search_id,
        })
        return _draft_result(result, "重复搜索结果已附加；请核对候选后明确确认：\n")
    if len(argv) == 4 and argv[1] == "create-confirm":
        draft_id, expected_digest = argv[2:]
        row = _owned_with_digest(conn, actor, draft_id, expected_digest)
        if row["duplicate_confirmed_at"] is not None:
            return _draft_result(row, "已确认不是重复缺陷，未重复修改：\n")
        result = execute(conn, config, action="confirm-create-not-duplicate", payload={
            "draft_id": draft_id, "expected_digest": expected_digest,
        })
        return _draft_result(result, "已记录明确的非重复确认：\n")
    if len(argv) == 4 and argv[1] == "create-ready":
        draft_id, expected_digest = argv[2:]
        row = _owned_with_digest(conn, actor, draft_id, expected_digest)
        if row["state"] == "ready":
            return _draft_result(row, "草稿已经就绪，未重复修改：\n")
        result = execute(conn, config, action="mark-create-ready", payload={
            "draft_id": draft_id, "expected_digest": expected_digest,
        })
        return _draft_result(result, "草稿已就绪；派发时仍会核验现有创建授权：\n")
    if len(argv) == 4 and argv[1] == "create-dispatch":
        draft_id, expected_digest = argv[2:]
        row = _owned_with_digest(conn, actor, draft_id, expected_digest)
        # The transport consumes the ready state before the one native create.
        # Replays and all uncertain/terminal states only return custody; no resend.
        if row["state"] != "ready":
            return _draft_result(row, "创建请求已处理，未重复发送：\n")
        result = execute(conn, config, action="dispatch-create-draft", payload={
            "draft_id": draft_id, "expected_digest": expected_digest,
        })
        prefix = "现有创建授权核验后已创建远端缺陷；可排队只读回查绑定：\n" if result["state"] == "created" else "创建结果未确认；不会重发：\n"
        return _draft_result(result, prefix)
    if len(argv) == 4 and argv[1] == "create-bind":
        draft_id, expected_digest = argv[2:]
        _owned_with_digest(conn, actor, draft_id, expected_digest)
        result = execute(conn, config, action="bind-created-draft", payload={
            "draft_id": draft_id, "expected_digest": expected_digest,
            "read_hours": 8, "local_priority": "P2",
        })
        return _result(
            f"已排队创建结果的只读回查与本地绑定：{result['intake_id']} · {result['state']}\n"
            "该读取不会再次创建远端缺陷。"
        )
    raise ControlError(_creation_help())
