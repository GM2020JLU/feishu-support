import pytest

from k3_support.knowledge import create_candidate
from k3_support.knowledge_inventory import page


def entry(conn, number, *, title="Pico 风扇", answer="操作说明"):
    return create_candidate(
        conn,
        title=title,
        questions=["怎么操作"],
        answer_markdown=answer,
        project="K3",
        module="EC",
        software_version="v1",
        disclosure_class="internal",
        confidence=0.9,
        source_authority=0.9,
        canonical_case_id=None,
        source_digest=f"inventory-{number}",
    )


def test_full_inventory_keyset_is_readonly_and_headers_only(conn):
    ids = {entry(conn, i) for i in range(67)}
    before = list(conn.iterdump())
    seen = []
    cursor = ""
    while True:
        result = page(conn, after_id=cursor)
        assert result["total_matching"] == 67
        assert result["live"] and not result["semantic_search"]
        assert len(result["items"]) <= 30
        for item in result["items"]:
            assert set(item) == {
                "knowledge_id",
                "title",
                "status",
                "project",
                "module",
                "review_due_at",
            }
            seen.append(item["knowledge_id"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert seen == sorted(ids)
    assert list(conn.iterdump()) == before


def test_filters_apply_before_pagination_and_queries_are_literal(conn):
    for i in range(35):
        entry(conn, i, title="Unrelated")
    target = entry(conn, 36, title="PICO 特殊 %_", answer="exact ' OR 1=1 --")
    conn.execute(
        "UPDATE knowledge_entries SET status='retired' WHERE knowledge_id=?", (target,)
    )
    for query in ["pico", "%_", "' OR 1=1 --", "特殊"]:
        result = page(conn, query=query, status="retired", limit=1)
        assert result["total_matching"] == 1
        assert result["items"][0]["knowledge_id"] == target
        assert result["next_cursor"] is None
    assert not page(conn, query="特殊", status="candidate")["items"]
    assert page(conn, query="EC")["total_matching"] == 36


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": []},
        {"query": "a" * 257},
        {"status": []},
        {"status": "published"},
        {"after_id": None},
        {"after_id": "a" * 101},
        {"limit": True},
        {"limit": 0},
        {"limit": 51},
    ],
)
def test_invalid_filters_rejected(conn, kwargs):
    with pytest.raises(ValueError):
        page(conn, **kwargs)


def test_deleted_cursor_does_not_break_next_page(conn):
    ids = sorted(entry(conn, i) for i in range(3))
    first = page(conn, limit=1)
    conn.execute("DELETE FROM knowledge_entries WHERE knowledge_id=?", (ids[0],))
    assert [
        r["knowledge_id"] for r in page(conn, after_id=first["next_cursor"])["items"]
    ] == ids[1:]
