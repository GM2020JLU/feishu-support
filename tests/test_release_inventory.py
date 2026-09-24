import json

import pytest

from k3_support.release_inventory import page


def test_release_inventory_is_read_only_filtered_and_bounded(conn):
    for index, repository in enumerate(["uboot", "ec", "uboot"]):
        conn.execute("""INSERT INTO release_impacts VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                     (str(index), repository, "change", str(index), "<script>title</script>",
                      "main", "[]", str(index), json.dumps({"summary": "test", "impact_level": "high",
                                                            "private": "not projected"}), None, "2026-09-08"))
    before = conn.total_changes
    first = page(conn, repository="uboot", limit=1)
    second = page(conn, repository="uboot", after_id=first["next_after_id"], limit=1)
    assert [first["items"][0]["impact_id"], second["items"][0]["impact_id"]] == ["0", "2"]
    assert second["next_after_id"] is None
    assert first["items"][0]["summary"] == "test"
    assert "private" not in str(first)
    assert conn.total_changes == before


@pytest.mark.parametrize("kwargs", [{"repository": []}, {"after_id": None}, {"limit": True}, {"limit": 51}])
def test_release_inventory_rejects_invalid_input(conn, kwargs):
    with pytest.raises(ValueError):
        page(conn, **kwargs)
