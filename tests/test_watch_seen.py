import pytest
from test_watch_races import seed

from k3_support.watch_subscriptions import mark_seen, page


@pytest.mark.parametrize("kind", ["release", "case"])
def test_seen_is_local_owned_idempotent_and_not_recollected(conn, kind):
    _, collect = seed(conn, kind)
    collect(conn)
    action = page(conn, owner_id="owner")["items"][0]["action_id"]
    cases = [tuple(row) for row in conn.execute("SELECT * FROM cases")]
    with pytest.raises(ValueError, match="not available"):
        mark_seen(conn, owner_id="other", action_id=action)
    first = mark_seen(conn, owner_id="owner", action_id=action)
    assert mark_seen(conn, owner_id="owner", action_id=action) == first
    assert page(conn, owner_id="owner")["items"] == []
    assert collect(conn)["created"] == 0
    assert [tuple(row) for row in conn.execute("SELECT * FROM cases")] == cases
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM watch_seen").fetchone()[0] == 1
