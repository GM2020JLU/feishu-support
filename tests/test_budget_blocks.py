from types import SimpleNamespace

from test_semantic_budget import policy

from k3_support import budget_blocks, model_budget, semantic, semantic_budget


def test_manifest_failure_deduplicates_without_retaining_private_input(
    conn, config, monkeypatch
):
    policy(conn)

    def invalid(path):
        raise ValueError("PRIVATE TOKEN")

    monkeypatch.setattr(semantic_budget, "manifest", invalid)
    call = semantic_budget.selector(
        conn, config, semantic.hermes_semantic_selector, scope="PRIVATE EVENT"
    )
    for _ in range(3):
        assert call("PRIVATE QUESTION", []) is None
    report = budget_blocks.report(conn)
    assert report["total_open"] == 1
    assert report["items"][0]["occurrences"] == 3
    assert report["items"][0]["reason"] == "identity_unverified"
    assert "PRIVATE" not in str(report) and not report["notification_sent"]
    monkeypatch.setattr(
        semantic_budget,
        "manifest",
        lambda path: {"model": "fixture", "provider": "fixture"},
    )
    monkeypatch.setattr(
        semantic.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout='{"knowledge_id":null,"confidence":0}'
        ),
    )
    assert call("PRIVATE QUESTION", []) is not None
    assert budget_blocks.report(conn)["total_open"] == 0
    assert conn.execute("SELECT count(*) FROM model_budget_blocks").fetchone()[0] == 1


def test_unconfirmed_result_preserves_charge_and_visible_next_step(
    conn, config, monkeypatch
):
    policy(conn)
    monkeypatch.setattr(
        semantic_budget,
        "manifest",
        lambda path: {"model": "fixture", "provider": "fixture"},
    )
    monkeypatch.setattr(
        semantic.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="private"),
    )
    call = semantic_budget.selector(
        conn, config, semantic.hermes_semantic_selector, scope="event"
    )
    assert call("question", []) is None
    report = model_budget.snapshot(conn)
    assert report["blocks"]["items"][0]["reason"] == "model_result_unconfirmed"
    assert report["charges"][0]["charged"] == 100
    assert "private" not in str(report)
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_report_is_bounded_and_has_exact_open_count(conn):
    for number in range(60):
        budget_blocks.record(conn, str(number), "fixture", "budget_gate_blocked")
    report = budget_blocks.report(conn)
    assert len(report["items"]) == 50 and report["total_open"] == 60
