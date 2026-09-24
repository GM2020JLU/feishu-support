from __future__ import annotations

import json
import stat

from k3_support.chat_history import backfill, extract_candidates


def test_history_backfill_and_private_candidate_extraction(conn, tmp_path, monkeypatch):
    calls = []

    def lark(args):
        calls.append(args)
        if "+chat-list" in args:
            return {
                "ok": True,
                "data": {"chats": [{"chat_id": "oc_private", "name": "support"}]},
                "meta": {"pagination": {"complete": True}},
            }
        return {
            "ok": True,
            "data": {
                "messages": [
                    {
                        "message_id": "om_q",
                        "create_time": "1",
                        "sender": {"id": "ou_peer"},
                        "body": {"content": json.dumps({"text": "K3 fastboot 为什么失败？ token=abc"})},
                    },
                    {
                        "message_id": "om_a",
                        "create_time": "2",
                        "sender": {"id": "ou_owner"},
                        "body": {"content": json.dumps({"text": "先看串口，再检查 10.0.0.8"})},
                    },
                ]
            },
            "meta": {"pagination": {"complete": True}},
        }

    monkeypatch.setattr("k3_support.chat_history._run_lark", lark)
    result = backfill(
        conn,
        data_dir=tmp_path,
        start_at="2025-09-02T00:00:00+08:00",
        end_at="2026-09-03T00:00:00+08:00",
    )
    assert result["state"] == "complete"
    archive = next((tmp_path / "chat-history" / result["run_id"] / "chats").iterdir())
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    report = tmp_path / "review" / "knowledge.md"
    extracted = extract_candidates(
        conn,
        run_id=result["run_id"],
        report_path=report,
        owner_open_id="ou_owner",
    )
    assert extracted["candidate_count"] == 1
    knowledge = conn.execute(
        "SELECT status,disclosure_class,question_variants_json,answer_markdown FROM knowledge_entries"
    ).fetchone()
    assert (knowledge["status"], knowledge["disclosure_class"]) == ("candidate", "private")
    assert "abc" not in knowledge["question_variants_json"]
    assert "10.0.0.8" not in knowledge["answer_markdown"]
    assert report.is_file()
