from __future__ import annotations

import json
import stat
import subprocess

import pytest

from k3_support import doc_history
from k3_support.doc_history import (
    DocHistoryError,
    backfill,
    build_review_report,
    import_link_manifest,
)


def test_doc_history_backfill_and_review_report(conn, tmp_path, monkeypatch):
    def lark(args, *, timeout=600):
        if "+search" in args:
            return {
                "ok": True,
                "data": {
                    "has_more": False,
                    "results": [
                        {
                            "entity_type": "DOCX",
                            "title_highlighted": "K3 UFS 调试",
                            "result_meta": {
                                "doc_types": "DOCX",
                                "token": "doc_1",
                                "url": "https://example/docx/doc_1",
                                "update_time_iso": "2026-09-01T00:00:00+08:00",
                            },
                        },
                        {
                            "entity_type": "WIKI",
                            "title_highlighted": "K3 数据表",
                            "result_meta": {
                                "doc_types": "SHEET",
                                "token": "wiki_2",
                                "url": "https://example/wiki/wiki_2",
                            },
                        },
                    ],
                },
            }
        return {
            "ok": True,
            "data": {
                "document": {
                    "document_id": "doc_1",
                    "revision_id": 7,
                    "content": "# K3 UFS\n\n随机读写压力测试需要关注队列深度。",
                }
            },
        }

    monkeypatch.setattr("k3_support.doc_history._run_lark", lark)
    result = backfill(conn, data_dir=tmp_path, workers=2)
    assert result["state"] == "complete"
    assert (result["total"], result["complete"], result["skipped"], result["k3"]) == (
        2,
        1,
        1,
        1,
    )
    archive = next((tmp_path / "doc-history" / result["run_id"] / "documents").iterdir())
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    report = tmp_path / "review" / "docs.md"
    built = build_review_report(
        conn, run_id=result["run_id"], report_path=report
    )
    assert built["document_count"] == 1
    raw_report = report.read_text(encoding="utf-8")
    assert "K3 UFS 调试" in raw_report
    assert "随机读写压力测试" not in raw_report
    source = conn.execute(
        "SELECT source_type,acl_json FROM source_registry WHERE stable_external_id='docx:doc_1'"
    ).fetchone()
    assert source["source_type"] == "feishu_doc"
    assert json.loads(source["acl_json"])["visibility"] == "private"

    manifest = tmp_path / "curated.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": result["run_id"],
                "routes": [
                    {
                        "key": "ufs-verify",
                        "questions": ["fio verify 是在哪里执行的？"],
                        "stable_id": "docx:doc_1",
                        "module": "UFS",
                        "confidence": 0.8,
                        "access_mode": "request_if_denied",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    link_report = tmp_path / "review" / "links.md"
    imported = import_link_manifest(
        conn, manifest_path=manifest, report_path=link_report
    )
    assert imported["route_count"] == 1
    report_text = link_report.read_text(encoding="utf-8")
    assert "K3 UFS 调试" in report_text
    assert "参考文档：[K3 UFS 调试](https://example/docx/doc_1)" in report_text
    assert "如提示无权限，请在文档页面申请访问" in report_text
    assert "随机读写压力测试" not in report_text
    knowledge = conn.execute(
        "SELECT status,disclosure_class,answer_markdown FROM knowledge_entries WHERE knowledge_id=?",
        (imported["knowledge_ids"][0],),
    ).fetchone()
    assert (knowledge["status"], knowledge["disclosure_class"]) == (
        "candidate",
        "internal",
    )
    assert knowledge["answer_markdown"] == (
        "参考文档：[K3 UFS 调试](https://example/docx/doc_1)"
        "\n\n如提示无权限，请在文档页面申请访问。"
    )
    assert conn.execute("SELECT count(*) FROM knowledge_sources").fetchone()[0] == 1

    repeated = import_link_manifest(
        conn, manifest_path=manifest, report_path=link_report
    )
    assert repeated["knowledge_ids"] == imported["knowledge_ids"]
    assert conn.execute("SELECT count(*) FROM knowledge_entries").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM knowledge_sources").fetchone()[0] == 1

    author_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    author_manifest["routes"][0]["access_mode"] = "author_share"
    manifest.write_text(json.dumps(author_manifest), encoding="utf-8")
    author_import = import_link_manifest(
        conn, manifest_path=manifest, report_path=link_report
    )
    assert author_import["knowledge_ids"] != imported["knowledge_ids"]
    author_answer = conn.execute(
        "SELECT answer_markdown FROM knowledge_entries WHERE knowledge_id=?",
        (author_import["knowledge_ids"][0],),
    ).fetchone()[0]
    assert author_answer == "参考文档：[K3 UFS 调试](https://example/docx/doc_1)"


def test_document_search_paginates_and_deduplicates(monkeypatch):
    calls = []

    def lark(args, *, timeout=600):
        calls.append(args)
        if "--page-token" not in args:
            return {
                "ok": True,
                "data": {
                    "has_more": True,
                    "page_token": "next-page",
                    "results": [
                        {
                            "entity_type": "DOCX",
                            "result_meta": {"token": "same", "url": "https://first"},
                        }
                    ],
                },
            }
        return {
            "ok": True,
            "data": {
                "has_more": False,
                "results": [
                    {
                        "entity_type": "DOCX",
                        "result_meta": {"token": "same", "url": "https://updated"},
                    },
                    {
                        "entity_type": "WIKI",
                        "result_meta": {"token": "other", "url": "https://other"},
                    },
                ],
            },
        }

    monkeypatch.setattr("k3_support.doc_history._run_lark", lark)
    documents = doc_history._search_created_documents()
    assert len(documents) == 2
    assert documents[0]["result_meta"]["url"] == "https://updated"
    assert calls[1][-2:] == ["--page-token", "next-page"]


def test_lark_timeout_is_safe_error(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="lark-cli", timeout=1)

    monkeypatch.setattr("k3_support.doc_history.subprocess.run", timeout)
    with pytest.raises(DocHistoryError, match="timed out"):
        doc_history._run_lark(["drive", "+search"], timeout=1)
