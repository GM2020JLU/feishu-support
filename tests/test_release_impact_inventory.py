import json

import pytest

from k3_support.release_impact_inventory import detail, page


def add_impact(conn, index):
    conn.execute(
        """INSERT INTO release_impacts(impact_id,repository,change_id,revision,subject,
               changed_paths_json,input_digest,assessment_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (f"imp-{index:03}", "software", str(index), str(index), "<script>untrusted</script>",
         json.dumps(["src/main.py"]), f"digest-{index}", json.dumps({"summary": "saved assessment"}),
         "2026-09-16T00:00:00+00:00"),
    )


def test_same_timestamp_pagination_has_no_duplicates_or_omissions(conn):
    for index in range(65):
        add_impact(conn, index)
    ids, cursor = [], ""
    before = conn.total_changes
    while True:
        result = page(conn, after_id=cursor)
        assert result["read_only"] and len(result["items"]) <= 30
        ids.extend(item["impact_id"] for item in result["items"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert ids == [f"imp-{index:03}" for index in reversed(range(65))]
    assert conn.total_changes == before


def test_detail_returns_saved_evidence_without_mutation(conn):
    add_impact(conn, 1)
    before = conn.total_changes
    result = detail(conn, impact_id="imp-001")
    assert result["assessment"] == {"summary": "saved assessment"}
    assert result["changed_paths"] == ["src/main.py"]
    assert result["subject"] == "<script>untrusted</script>"
    assert result["read_only"] and conn.total_changes == before


@pytest.mark.parametrize("cursor", [None, [], "x" * 129, "missing"])
def test_invalid_cursor_does_not_restart_at_first_page(conn, cursor):
    with pytest.raises(ValueError):
        page(conn, after_id=cursor)
