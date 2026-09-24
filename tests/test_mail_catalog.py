from __future__ import annotations

from k3_support.lark import CommandResult
from k3_support.mail_catalog import (
    MailCatalogError,
    _reclassify_unresolved,
    catalog_overview,
    query_catalog,
    scan_mail_catalog,
)
from k3_support.store import ingest_event


def _classifier(value):
    items = []
    for message in value["messages"]:
        upstream = "[PATCH" in str(message["subject"])
        items.append(
            {
                "message_id": message["message_id"],
                "category": "upstream" if upstream else "build_ci",
                "origin": "upstream" if upstream else "automation",
                "attention": "information",
                "topics": ["linux_kernel" if upstream else "build_infra"],
                "confidence": 0.97,
            }
        )
    return {"items": items}


def test_mail_catalog_is_resumable_and_never_persists_body(conn, config):
    config.raw["features"]["mail"] = True
    calls = []

    def runner(argv):
        calls.append(argv)
        if argv[0:2] == ["mail", "+messages"]:
            message_id = argv[argv.index("--message-ids") + 1]
            return CommandResult(
                {
                    "messages": [
                        {
                            "message_id": message_id,
                            "thread_id": f"thread-{message_id}",
                            "subject": (
                                "[PATCH] ufs update"
                                if message_id == "mail-2"
                                else "Build successful"
                            ),
                            "body_plain_text": "PRIVATE BODY MUST NOT BE STORED",
                            "internal_date": "1788420000000",
                            "head_from": {
                                "name": "sender",
                                "mail_address": "sender@example.com",
                            },
                        }
                    ]
                },
                "user",
                [],
            )
        folder = argv[argv.index("--folder-id") + 1]
        token = argv[argv.index("--page-token") + 1] if "--page-token" in argv else None
        if folder == "INBOX" and token is None:
            return CommandResult(
                {
                    "messages": [
                        {
                            "message_id": "mail-1",
                            "thread_id": "thread-mail-1",
                            "folder": "INBOX",
                            "from": "CI <ci@example.com>",
                            "subject": "Build successful",
                            "date": "2026-09-03 10:00",
                        }
                    ],
                    "has_more": True,
                    "page_token": "token-2",
                },
                "user",
                [],
            )
        if folder == "INBOX" and token == "token-2":
            return CommandResult(
                {
                    "messages": [
                        {
                            "message_id": "mail-2",
                            "thread_id": "thread-mail-2",
                            "folder": "INBOX",
                            "from": "List <list@kernel.org>",
                            "subject": "[PATCH] ufs update",
                            "date": "2026-09-02 10:00",
                        }
                    ],
                    "has_more": False,
                },
                "user",
                [],
            )
        assert folder == "ARCHIVED"
        return CommandResult({"messages": [], "has_more": False}, "user", [])

    first = scan_mail_catalog(
        conn, config, runner=runner, classifier=_classifier, max_pages=1
    )
    second = scan_mail_catalog(
        conn, config, runner=runner, classifier=_classifier, max_pages=1
    )
    third = scan_mail_catalog(
        conn, config, runner=runner, classifier=_classifier, max_pages=1
    )

    assert first["has_cursor"] is True
    assert first["page_size"] == 100
    assert second["folder_index"] == 1
    assert third["state"] == "complete"
    assert catalog_overview(conn)["categories"] == {"build_ci": 1, "upstream": 1}
    assert query_catalog(conn, topic="ufs")[0]["message_id"] == "mail-2"
    assert query_catalog(conn, category="upstream")[0]["message_id"] == "mail-2"
    assert "PRIVATE BODY MUST NOT BE STORED" not in "\n".join(conn.iterdump())
    assert any("--page-token" in call for call in calls)

    ingest_event(
        conn,
        source="feishu_mail",
        identity="user",
        external_id="mail-live:received",
        payload={
            "message_id": "mail-live",
            "subject": "Build successful",
            "body_plain_text": "new realtime mail",
            "internal_date": "1788421000000",
        },
        occurred_at="2026-09-03T10:00:00+00:00",
    )
    incremental = scan_mail_catalog(
        conn, config, runner=runner, classifier=_classifier, max_pages=1
    )
    assert incremental["incremental_indexed"] == 1
    assert conn.execute(
        "SELECT category FROM mail_catalog_items WHERE message_id='mail-live'"
    ).fetchone()[0] == "build_ci"


def test_mail_catalog_run_keeps_its_original_page_size(conn, config):
    config.raw["features"]["mail"] = True
    calls = []

    def runner(argv):
        calls.append(argv)
        if argv[0:2] == ["mail", "+messages"]:
            return CommandResult({"messages": []}, "user", [])
        return CommandResult(
            {"messages": [], "has_more": True, "page_token": "next-2" if '--page-token' in argv else "next"},
            "user",
            [],
        )

    first = scan_mail_catalog(
        conn, config, runner=runner, classifier=_classifier, page_size=25
    )
    second = scan_mail_catalog(
        conn, config, runner=runner, classifier=_classifier, page_size=100
    )
    restarted = scan_mail_catalog(
        conn,
        config,
        runner=runner,
        classifier=_classifier,
        page_size=100,
        restart=True,
    )

    assert first["page_size"] == 25
    assert second["page_size"] == 25
    assert restarted["page_size"] == 100
    max_values = [call[call.index("--max") + 1] for call in calls]
    assert max_values == ["25", "25", "100"]


def test_mail_catalog_classifier_failure_does_not_advance_cursor(conn, config):
    config.raw["features"]["mail"] = True

    def runner(argv):
        if argv[0:2] == ["mail", "+triage"]:
            return CommandResult(
                {
                    "messages": [
                        {
                            "message_id": "mail-real",
                            "folder": "INBOX",
                            "subject": "Status",
                        }
                    ],
                    "has_more": True,
                    "page_token": "next",
                },
                "user",
                [],
            )
        return CommandResult(
            {
                "messages": [
                    {
                        "message_id": "mail-real",
                        "subject": "Status",
                        "body_plain_text": "body",
                    }
                ]
            },
            "user",
            [],
        )

    def invalid(_value):
        return {
            "items": [
                {
                    "message_id": "mail-invented",
                    "category": "not-a-category",
                    "origin": "unknown",
                    "attention": "information",
                    "topics": ["other"],
                    "confidence": 0.9,
                }
            ]
        }

    try:
        scan_mail_catalog(conn, config, runner=runner, classifier=invalid)
    except MailCatalogError:
        pass
    else:
        raise AssertionError("invalid classifier output must fail")

    run = conn.execute("SELECT * FROM mail_catalog_runs").fetchone()
    assert run["state"] == "failed"
    assert run["pages_processed"] == 0
    assert run["next_page_token"] is None
    assert conn.execute("SELECT count(*) FROM mail_catalog_items").fetchone()[0] == 0


def test_mail_catalog_retries_invalid_ai_batch_as_exact_singletons(conn, config):
    config.raw["features"]["mail"] = True
    calls = []

    def runner(argv):
        if argv[0:2] == ["mail", "+triage"]:
            return CommandResult(
                {
                    "messages": [
                        {"message_id": "mail-a", "subject": "Question A"},
                        {"message_id": "mail-b", "subject": "Question B"},
                    ],
                    "has_more": False,
                },
                "user",
                [],
            )
        ids = argv[argv.index("--message-ids") + 1].split(",")
        return CommandResult(
            {
                "messages": [
                    {"message_id": item, "subject": f"Question {item}"}
                    for item in ids
                ]
            },
            "user",
            [],
        )

    def classifier(value):
        calls.append([item["message_id"] for item in value["messages"]])
        messages = value["messages"]
        if len(messages) > 1:
            messages = messages[:1]
        return {
            "items": [
                {
                    "message_id": (
                        "model-copied-the-id-wrong"
                        if len(value["messages"]) == 1
                        else item["message_id"]
                    ),
                    "category": "support_bug",
                    "origin": "internal_human",
                    "attention": "action_required",
                    "topics": ["k3_platform"],
                    "confidence": 0.9,
                }
                for item in messages
            ]
        }

    result = scan_mail_catalog(
        conn, config, runner=runner, classifier=classifier, max_pages=1
    )

    assert result["messages_seen"] == 2
    assert calls == [["mail-a", "mail-b"], ["mail-a"], ["mail-b"]]
    assert conn.execute(
        "SELECT count(*) FROM mail_catalog_items WHERE classification_source='ai_retry_v1'"
    ).fetchone()[0] == 2


def test_mail_catalog_retries_transient_invalid_singleton_schema(conn, config):
    config.raw["features"]["mail"] = True
    calls = 0

    def runner(argv):
        if argv[0:2] == ["mail", "+triage"]:
            return CommandResult(
                {
                    "messages": [{"message_id": "mail-retry", "subject": "Question"}],
                    "has_more": False,
                },
                "user",
                [],
            )
        return CommandResult(
            {"messages": [{"message_id": "mail-retry", "subject": "Question"}]},
            "user",
            [],
        )

    def classifier(_value):
        nonlocal calls
        calls += 1
        if calls < 3:
            return None
        return {
            "items": [
                {
                    "message_id": "mail-retry",
                    "category": "support_bug",
                    "origin": "internal_human",
                    "attention": "action_required",
                    "topics": ["k3_platform"],
                    "confidence": 0.91,
                }
            ]
        }

    result = scan_mail_catalog(
        conn, config, runner=runner, classifier=classifier, max_pages=1
    )

    assert result["messages_seen"] == 1
    assert calls == 3


def test_mail_catalog_surfaces_repeated_missing_schema_for_review(conn, config):
    config.raw["features"]["mail"] = True

    def runner(argv):
        if argv[0:2] == ["mail", "+triage"]:
            return CommandResult(
                {
                    "messages": [{"message_id": "mail-unresolved", "subject": "Hello"}],
                    "has_more": False,
                },
                "user",
                [],
            )
        return CommandResult(
            {"messages": [{"message_id": "mail-unresolved", "subject": "Hello"}]},
            "user",
            [],
        )

    result = scan_mail_catalog(
        conn,
        config,
        runner=runner,
        classifier=lambda _value: None,
        max_pages=1,
    )

    assert result["messages_seen"] == 1
    assert catalog_overview(conn)["needs_review"] == 1
    rows = query_catalog(conn, needs_review=True)
    assert len(rows) == 1
    assert rows[0]["message_id"] == "mail-unresolved"
    assert rows[0]["category"] == "other"
    assert rows[0]["attention"] == "action_required"
    assert rows[0]["confidence"] == 0.0
    assert rows[0]["classification_source"] == "ai_unresolved_v1"

    conn.execute(
        "UPDATE mail_catalog_items SET subject='[RFC PATCH] riscv: add K3 support'"
    )
    assert _reclassify_unresolved(conn) == 1
    reviewed = query_catalog(conn, category="upstream")
    assert len(reviewed) == 1
    assert reviewed[0]["classification_source"] == "rules_v1"
    assert catalog_overview(conn)["needs_review"] == 0
