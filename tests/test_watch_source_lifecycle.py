import pytest
from test_watch_races import seed

from k3_support.watch_subscriptions import page


@pytest.mark.parametrize("kind", ["release", "case"])
@pytest.mark.parametrize("mutation", ["delete", "retarget"])
def test_watch_hides_missing_or_retargeted_source_without_mutation(conn, kind, mutation):
    _, collect = seed(conn, kind)
    collect(conn)
    original = page(conn, owner_id="owner")["items"][0]
    if kind == "release":
        if mutation == "delete":
            conn.execute("DELETE FROM release_impacts WHERE impact_id=?", (original["source_id"],))
        else:
            conn.execute("UPDATE release_impacts SET repository='another' WHERE impact_id=?", (original["source_id"],))
    elif mutation == "delete":
        conn.execute("DELETE FROM case_events WHERE event_id=?", (original["source_id"],))
    else:
        from k3_support.store import create_case

        other, _ = create_case(conn, title="other", case_type="bug", severity="P3", confidence=0.8)
        conn.execute("UPDATE case_events SET case_id=?,sequence=2 WHERE event_id=?", (other, original["source_id"]))
    before = list(conn.iterdump())
    assert page(conn, owner_id="owner")["items"] == []
    assert list(conn.iterdump()) == before
    assert conn.execute("SELECT count(*) FROM watch_actions").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
