from __future__ import annotations

import pytest

from k3_support.incidents import (
    attach_similar_case,
    diagnostic_context_records,
    record_diagnostic_snapshot,
)
from k3_support.store import create_case


def test_semantic_similarity_clusters_without_merging_case_ownership(conn):
    canonical, _ = create_case(
        conn,
        title="UFS 初始化后找不到 rootfs 分区",
        case_type="bug",
        severity="P2",
        confidence=0.8,
    )
    related, _ = create_case(
        conn,
        title="从 UFS 启动时根文件系统分区不可见",
        case_type="bug",
        severity="P2",
        confidence=0.8,
    )

    cluster = attach_similar_case(
        conn,
        case_id=related,
        query="UFS 可以 probe，但启动 Linux 后 rootfs UUID 找不到",
        selector=lambda value: {
            "canonical_case_id": canonical,
            "confidence": 0.96,
            "reason": "同为 UFS probe 成功后 rootfs 分区不可见",
        },
    )

    assert cluster["canonical_case_id"] == canonical
    assert conn.execute(
        "SELECT count(*) FROM incident_cluster_members WHERE cluster_id=?",
        (cluster["cluster_id"],),
    ).fetchone()[0] == 2
    assert tuple(
        conn.execute(
            "SELECT state,canonical_case_id FROM cases WHERE case_id=?", (related,)
        ).fetchone()
    ) == ("intake", None)


def test_similarity_rejects_generic_or_invented_case_match(conn):
    create_case(
        conn, title="启动失败", case_type="bug", severity="P2", confidence=0.8
    )
    candidate, _ = create_case(
        conn, title="另一个启动失败", case_type="bug", severity="P2", confidence=0.8
    )
    assert attach_similar_case(
        conn,
        case_id=candidate,
        query="启动失败",
        selector=lambda _: {
            "canonical_case_id": "K3-invented",
            "confidence": 0.99,
            "reason": "generic wording",
        },
    ) is None
    assert conn.execute("SELECT count(*) FROM incident_clusters").fetchone()[0] == 0


def test_diagnostic_snapshot_extracts_bounded_facts_and_missing_fields(conn):
    case_id, _ = create_case(
        conn, title="boot", case_type="bug", severity="P2", confidence=0.8
    )
    event_pk = conn.execute(
        """INSERT INTO inbound_events(event_pk,source,identity,external_id,idempotency_key,
               occurred_at,occurred_epoch,received_at,received_epoch,payload_json,status)
           VALUES('evt_diag','feishu_user_poll','user','om_diag','diag-idem',
                  '2026-09-03T00:00:00+00:00',1,'2026-09-03T00:00:00+00:00',1,'{}','processed')
           RETURNING event_pk"""
    ).fetchone()[0]
    snapshot = record_diagnostic_snapshot(
        conn,
        case_id=case_id,
        event_pk=event_pk,
        content="Pico 上使用 1.2 镜像从 UFS 启动，停在 Loading kernel",
        extractor=lambda value: {
            "facts": {
                "hardware": "Pico",
                "software_version": "1.2",
                "boot_media": "UFS",
                "boot_stage": "Loading kernel",
            },
            "missing": ["error_markers", "reproduction_steps"],
            "confidence": 0.94,
        },
    )

    assert snapshot["facts"]["boot_media"] == "UFS"
    reports = diagnostic_context_records(conn, case_id=case_id)
    assert len(reports) == 1 and reports[0]["event_pk"] == event_pk
    assert reports[0]["facts"] == snapshot["facts"]
    def must_not_call(value):
        raise AssertionError("replay must not call diagnostic model")

    before_replay = list(conn.iterdump())
    replay = record_diagnostic_snapshot(
        conn, case_id=case_id, event_pk=event_pk, content="Pico 上使用 1.2 镜像从 UFS 启动，停在 Loading kernel",
        extractor=must_not_call,
    )
    assert replay == snapshot
    assert list(conn.iterdump()) == before_replay
    conn.execute("UPDATE inbound_events SET payload_json=? WHERE event_pk=?", ('{"edited":true}', event_pk))
    before_read = list(conn.iterdump())
    assert diagnostic_context_records(conn, case_id=case_id) == []
    assert list(conn.iterdump()) == before_read
    changed = record_diagnostic_snapshot(conn, case_id=case_id, event_pk=event_pk,
        content="corrected source", extractor=lambda _: {"facts": {"boot_media": "NVMe"}, "missing": [], "confidence": 0.7})
    assert changed["snapshot_id"] != snapshot["snapshot_id"]
    assert changed["facts"]["boot_media"] == "NVMe"
    assert [value["facts"] for value in diagnostic_context_records(conn, case_id=case_id)] == [changed["facts"]]
    other_case, _ = create_case(conn, title="other", case_type="bug", severity="P2", confidence=0.8)
    assert record_diagnostic_snapshot(
        conn, case_id=other_case, event_pk=event_pk, content="same source",
        extractor=must_not_call,
    ) is None
    assert snapshot["missing"] == ["error_markers", "reproduction_steps"]
    assert conn.execute(
        "SELECT count(*) FROM diagnostic_snapshots WHERE case_id=?", (case_id,)
    ).fetchone()[0] == 2


@pytest.mark.parametrize("missing", [[[]], [{}], [1], [None], ["hardware", "hardware"]])
def test_invalid_diagnostic_missing_fields_do_not_raise_or_write(conn, missing):
    conn.execute(
        """INSERT INTO inbound_events(event_pk,source,identity,external_id,idempotency_key,
               occurred_at,occurred_epoch,received_at,received_epoch,payload_json,status)
           VALUES('unused','feishu_user_poll','user','invalid_diag','invalid-diag',
                  '2026-09-03T00:00:00+00:00',1,'2026-09-03T00:00:00+00:00',1,'{}','processed')"""
    )
    before = list(conn.iterdump())
    assert record_diagnostic_snapshot(
        conn, case_id="unused", event_pk="unused", content="synthetic",
        extractor=lambda value: {"facts": {}, "missing": missing, "confidence": 0.9},
    ) is None
    assert list(conn.iterdump()) == before
