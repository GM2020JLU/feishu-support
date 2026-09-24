from uuid import uuid4

from k3_support.broker_launch_status import snapshot


def test_status_is_read_only_and_not_a_health_claim(conn):
    assert snapshot(conn)["unresolved"] == []
    for state in ["launching", "accepted", "unknown", "finished"]:
        conn.execute("INSERT INTO broker_launches VALUES(?,?,?,?)", (str(uuid4()), state, "synthetic", "synthetic"))
    before = list(conn.iterdump())
    result = snapshot(conn)
    assert result["read_only"] and len(result["unresolved"]) == 3
    assert {row["state"] for row in result["unresolved"]} == {"launching", "accepted", "unknown"}
    assert "不会自动重试" in next(row["label"] for row in result["unresolved"] if row["state"] == "unknown")
    assert list(conn.iterdump()) == before
