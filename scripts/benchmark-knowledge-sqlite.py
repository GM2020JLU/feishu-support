#!/usr/bin/env python3
"""Synthetic SQLite retrieval plus concurrent real communication-claim benchmark.

No model calls, external transports, human Gold or production data. The output
must be a new directory inside Codex artifacts or the system temp directory.
"""
import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import tempfile
import threading
import time

from k3_support.coordination import control_communication, ensure_turn
from k3_support.db import connect, migrate, transaction
from k3_support.knowledge import create_candidate, review
from k3_support.knowledge_runtime import query_knowledge
from k3_support.store import create_case, ingest_event
import k3_support.knowledge_runtime as runtime_module


def stats(values):
    ordered = sorted(values)
    return {"samples": len(values), "p50_ms": round(ordered[math.ceil(len(values)*.5)-1], 3),
            "p95_ms": round(ordered[math.ceil(len(values)*.95)-1], 3),
            "max_ms": round(ordered[-1], 3)}


def populate(conn, size):
    # Synthetic approved entries do not assert human review or answer quality.
    for index in range(size):
        title = f"UFS storage fixture_{index:05d}"
        key = create_candidate(conn, title=title, questions=[title], answer_markdown="Synthetic timing fixture only",
                               project="K3", module="ufs", software_version=None, disclosure_class="public",
                               confidence=.99, source_authority=.99, canonical_case_id=None, source_digest=f"fixture-{index}")
        review(conn, knowledge_id=key, reviewer_id="synthetic-benchmark", decision="approved")
    event, _ = ingest_event(conn, source="feishu_user_poll", identity="user", external_id="fixture-question",
                            payload={"content": "UFS storage", "chat_type": "p2p", "message_type": "text"},
                            occurred_at="2026-09-08T00:00:00+00:00", sender_id="fixture-peer", chat_id="fixture-chat")
    case, _ = create_case(conn, title="Synthetic timing fixture", case_type="bug", severity="P3", confidence=.5,
                          requester_id="fixture-peer", requester_chat_id="fixture-chat", source_event_pk=event)
    with transaction(conn):
        ensure_turn(conn, case_id=case, source_event_pk=event)
    return case


def claim(path, case, index, started, finished):
    conn = connect(path)
    try:
        if not started.wait(60):
            raise RuntimeError("retrieval did not reach its SQL scan")
        overlap = not finished.is_set()
        begin = time.perf_counter()
        conn.execute("BEGIN IMMEDIATE")
        acquired = time.perf_counter()
        try:
            control_communication(conn, case_id=case, action="claim", actor_id="synthetic-owner",
                                  external_id=f"benchmark-claim-{index}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        end = time.perf_counter()
        return {"overlapped": overlap, "lock_wait_ms": (acquired-begin)*1000,
                "write_transaction_ms": (end-acquired)*1000, "claim_ms": (end-begin)*1000}
    finally:
        conn.close()


def measure(directory, size, samples):
    path = directory / f"synthetic-{size}.db"
    conn = connect(path)
    migrate(conn)
    try:
        case = populate(conn, size)
        latencies, claims, selected = [], [], 0
        # Warm page/cache run is excluded and explicitly labeled in the report.
        query_knowledge(conn, query="UFS storage fixture_00000", requester_id="fixture-peer", chat_id="fixture-chat")
        with ThreadPoolExecutor(max_workers=1) as pool:
            for index in range(samples):
                started, finished = threading.Event(), threading.Event()

                def traced(sql):
                    if "FROM knowledge_entries ke" in sql:
                        started.set()

                conn.set_trace_callback(traced)
                future = pool.submit(claim, path, case, index, started, finished)
                begin = time.perf_counter()
                try:
                    result = query_knowledge(conn, query=f"UFS storage fixture_{index % size:05d}",
                                             requester_id="fixture-peer", chat_id="fixture-chat")
                    latencies.append((time.perf_counter()-begin)*1000)
                    selected += result["selected_knowledge_id"] is not None
                finally:
                    finished.set()
                    started.set()
                    conn.set_trace_callback(None)
                claims.append(future.result(timeout=60))
        if conn.execute("SELECT communication_owner FROM conversation_turns").fetchone()[0] != "human":
            raise RuntimeError("real communication claim did not take effect")
        return {"entries": size, "query": stats(latencies), "selection_count_not_quality": selected,
                "overlapped_claims": sum(item["overlapped"] for item in claims),
                **{key: stats([item[key] for item in claims]) for key in ("lock_wait_ms", "write_transaction_ms", "claim_ms")}}
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000])
    parser.add_argument("--samples", type=int, default=20)
    args = parser.parse_args()
    if not 5 <= args.samples <= 100 or any(size not in {100, 1000, 10000} for size in args.sizes):
        parser.error("samples must be 5..100 and sizes 100/1000/10000")
    output = Path(args.output).expanduser().resolve()
    artifacts = Path(os.environ.get("XDG_DATA_HOME", str(Path.home()/".local/share"))) / "codex/artifacts"
    if not any(output != root and output.is_relative_to(root) for root in (artifacts.resolve(), Path(tempfile.gettempdir()).resolve())):
        parser.error("output must be a new directory under Codex artifacts or system temp")
    output.mkdir(mode=0o700)
    report = {"scope": "synthetic SQLite query_knowledge with real concurrent communication claims",
              "knowledge_runtime_sha256": hashlib.sha256(Path(runtime_module.__file__).read_bytes()).hexdigest(),
              "warmup_queries_per_size": 1, "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
              "external_calls": False, "production_data": False, "semantic_quality_verified": False,
              "excluded": ["model_latency", "Qdrant", "professional_publication_validation", "cold_cache"],
              "results": []}
    for size in args.sizes:
        result = measure(output, size, args.samples)
        report["results"].append(result)
        print(json.dumps(result), flush=True)
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
