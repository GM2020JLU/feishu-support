from types import SimpleNamespace

import pytest

from k3_support import model_budget, semantic, semantic_budget


@pytest.mark.parametrize("command", ["catalog", "summary", "release"])
def test_cli_model_callbacks_use_budget(conn, config, monkeypatch, command):
    from k3_support import cli, mail, mail_catalog, release_impact

    policy(conn)
    config.raw["mode"] = "active"
    config.raw["features"]["mail"] = True
    monkeypatch.setattr(cli, "_config", lambda args: config)
    monkeypatch.setattr(cli, "_conn", lambda args: conn)
    monkeypatch.setattr(cli, "_emit", lambda value: None)
    monkeypatch.setattr(
        semantic_budget,
        "manifest",
        lambda path: {"model": "fixture", "provider": "fixture"},
    )
    calls = []

    def process(*args, **kwargs):
        import json

        from k3_support.ids import digest

        assert json.loads(kwargs["input"])["expected_manifest_digest"] == digest(
            {"model": "fixture", "provider": "fixture"}
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM model_budget_attempts WHERE state='dispatched'"
            ).fetchone()[0]
            == 1
        )
        calls.append(1)
        return SimpleNamespace(returncode=0, stdout="{}")

    monkeypatch.setattr(semantic.subprocess, "run", process)
    if command == "catalog":
        monkeypatch.setattr(
            mail_catalog,
            "scan_mail_catalog",
            lambda conn, cfg, **kwargs: kwargs["classifier"]({"items": []}),
        )
        invoke = lambda: cli.cmd_mail_catalog_scan(
            SimpleNamespace(max_pages=1, page_size=10, restart=False)
        )
    elif command == "summary":
        monkeypatch.setattr(mail, "refresh_missing_bodies", lambda *args, **kwargs: {})
        monkeypatch.setattr(
            mail,
            "prepare_summary",
            lambda conn, **kwargs: kwargs["summarizer"]({"items": []}),
        )
        invoke = lambda: cli.cmd_mail_summary(
            SimpleNamespace(
                scheduled_at="2026-09-07T12:00:00+08:00",
                destination="telegram:owner",
                slot="mail_noon",
            )
        )
    else:
        monkeypatch.setattr(
            release_impact,
            "assess_release_change",
            lambda conn, cfg, **kwargs: kwargs["analyzer"]({"change": "synthetic"}),
        )
        invoke = lambda: cli.cmd_release_impact(
            SimpleNamespace(change='{"id":"synthetic"}')
        )
    invoke()
    assert calls == [1]
    assert semantic.expected_bridge_manifest.get() is None


def test_readonly_budget_connection_never_bypasses_precharge(conn, config, monkeypatch):
    import sqlite3

    policy(conn)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    readonly = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
    readonly.row_factory = sqlite3.Row
    monkeypatch.setattr(
        semantic_budget,
        "manifest",
        lambda path: {"model": "fixture", "provider": "fixture"},
    )
    monkeypatch.setattr(
        semantic.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("unbudgeted call"),
    )
    try:
        call = semantic_budget.selector(
            readonly, config, semantic.hermes_semantic_selector, scope="readonly"
        )
        assert call("question", []) is None
    finally:
        readonly.close()
    assert conn.execute("SELECT count(*) FROM model_budget_attempts").fetchone()[0] == 0


def policy(conn):
    model_budget.configure(
        conn,
        currency="USD",
        daily_limit=200,
        case_limit=200,
        attempt_limit=100,
        actor_id="owner",
        expected_revision=0,
    )


@pytest.mark.parametrize("failure", [None, "endpoint", "alias", "manifest"])
def test_configured_identity_is_checked_without_budget_policy(conn, config, monkeypatch, failure):
    import json

    from k3_support.ids import digest

    identity = {"schema_version": 2, "model": "fixture", "provider": "custom:chosen",
                "runtime_provider": "custom", "endpoint_sha256": "a" * 64}
    monkeypatch.setenv("K3_SUPPORT_HERMES_BRIDGE_CONFIG", "/fixture/private.json")
    reads = []
    def read(path):
        reads.append(path)
        return {**identity, "model": "changed"} if failure == "manifest" and len(reads) > 1 else identity
    monkeypatch.setattr(semantic_budget, "manifest", read)
    calls = []
    def run(argv, **kwargs):
        calls.append(1)
        payload = json.loads(kwargs["input"])
        assert payload["protocol"] == 2
        assert payload["expected_manifest_digest"] == digest(identity)
        receipt = {"model": "fixture", "response_model": "fixture", "provider": "custom",
                   "requested_provider": "wrong" if failure == "alias" else "custom:chosen",
                   "endpoint_sha256": "b" * 64 if failure == "endpoint" else "a" * 64,
                   "finish_reason": "stop", "api_request_id": "request", "session_id": "session"}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"protocol": 2,
            "manifest_digest": digest(identity), "request_digest": digest(payload), "receipt": receipt,
            "result": {"knowledge_id": None, "confidence": 0}}))
    monkeypatch.setattr(semantic.subprocess, "run", run)
    call = semantic_budget.selector(conn, config, semantic.hermes_semantic_selector, scope="no-budget")
    assert (call("question", []) is not None) is (failure is None)
    assert len(calls) == (0 if failure == "manifest" else 1)
    assert conn.execute("SELECT count(*) FROM model_budget_attempts").fetchone()[0] == 0
    assert semantic.expected_bridge_identity.get() is None
    assert semantic.expected_bridge_manifest.get() is None


def test_real_semantic_entry_reserves_before_process_and_stops_replay(
    conn, config, monkeypatch
):
    policy(conn)
    monkeypatch.setattr(
        semantic_budget,
        "manifest",
        lambda path: {"provider": "fixture", "model": "fixture"},
    )
    calls = []

    def run(*args, **kwargs):
        row = conn.execute(
            "SELECT state,charged FROM model_budget_attempts WHERE state='dispatched' LIMIT 1"
        ).fetchone()
        assert row["state"] == "dispatched" and row["charged"] == 100
        calls.append(args)
        return SimpleNamespace(
            returncode=0, stdout='{"knowledge_id":null,"confidence":0}'
        )

    monkeypatch.setattr(semantic.subprocess, "run", run)
    first = semantic_budget.selector(
        conn, config, semantic.hermes_semantic_selector, scope="event1"
    )
    assert first("question", [])["confidence"] == 0
    assert first("question", []) is None
    second = semantic_budget.selector(
        conn, config, semantic.hermes_semantic_selector, scope="event2"
    )
    assert second("question", []) is not None
    third = semantic_budget.selector(
        conn, config, semantic.hermes_semantic_selector, scope="event3"
    )
    assert third("question", []) is None
    assert len(calls) == 2
    assert (
        conn.execute("SELECT sum(charged) FROM model_budget_attempts").fetchone()[0]
        == 200
    )
    assert semantic.budget_transport.get() is None


def test_missing_identity_blocks_configured_budget_without_process(
    conn, config, monkeypatch
):
    policy(conn)

    def invalid(path):
        raise ValueError("invalid private manifest")

    monkeypatch.setattr(semantic_budget, "manifest", invalid)
    monkeypatch.setattr(
        semantic.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call")),
    )
    call = semantic_budget.selector(
        conn, config, semantic.hermes_semantic_selector, scope="event"
    )
    assert call("question", []) is None
    assert conn.execute("SELECT count(*) FROM model_budget_attempts").fetchone()[0] == 0


def test_timeout_retains_charge_and_unconfigured_path_remains_unchanged(
    conn, config, monkeypatch
):
    import subprocess

    calls = []

    def timeout(*args, **kwargs):
        calls.append(1)
        raise subprocess.TimeoutExpired("synthetic", 1)

    monkeypatch.setattr(semantic.subprocess, "run", timeout)
    call = semantic_budget.selector(
        conn, config, semantic.hermes_semantic_selector, scope="event"
    )
    assert call("question", []) is None
    assert len(calls) == 1
    policy(conn)
    monkeypatch.setattr(
        semantic_budget,
        "manifest",
        lambda path: {"provider": "fixture", "model": "fixture"},
    )
    assert call("question", []) is None
    assert call("question", []) is None
    assert len(calls) == 2
    row = conn.execute("SELECT state,charged FROM model_budget_attempts").fetchone()
    assert row["state"] == "unknown" and row["charged"] == 100
