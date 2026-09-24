from k3_support.db import connect, migrate, migration_files
from k3_support.store import ingest_event


def test_pre_runtime_route_migrates_without_inventing_provenance(tmp_path):
    conn = connect(tmp_path / "before-runtime.db")
    try:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT,applied_at TEXT)"
        )
        for version, name, sql in migration_files():
            if version >= 28:
                break
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations VALUES(?,?,?)",
                (version, name, "2026-01-01T00:00:00+00:00"),
            )
        event, _ = ingest_event(
            conn,
            source="feishu_user_poll",
            identity="user",
            external_id="old-route",
            payload={"content": "old query"},
            occurred_at="2026-01-01T00:00:00+00:00",
            sender_id="ou_peer",
            chat_id="oc_peer",
        )
        conn.execute(
            """INSERT INTO route_decisions
            (route_decision_id,event_pk,route,proposed_route,confidence,issue_type,severity,domain,
             requires_owner_judgment,profile_snapshot_json,model_output_digest,created_at)
            VALUES('old',?,'direct_answer','direct_answer',0.99,'faq','P3','boot',0,'{}','old',
                   '2026-01-01T00:00:00+00:00')""",
            (event,),
        )
        assert 28 in migrate(conn)
        row = conn.execute(
            "SELECT route,knowledge_runtime_json FROM route_decisions WHERE route_decision_id='old'"
        ).fetchone()
        assert tuple(row) == ("direct_answer", "{}")
        assert migrate(conn) == []
    finally:
        conn.close()
