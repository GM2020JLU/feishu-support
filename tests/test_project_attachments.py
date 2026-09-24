# ruff: noqa: F811 -- shared isolated fixtures
import copy
import json

import pytest
from test_project_activity import context  # noqa: F401
from test_project_read_snapshot import DEST, Client, bound, responses  # noqa: F401

from k3_support import project_activity as activity
from k3_support.project_attachments_reader import AttachmentsReader
from k3_support.project_read_client import ProjectReadError

FILE = {
    "name": "<script>firmware.zip</script>",
    "size": "67.6MB",
    "type": "application/zip",
    "url": "private-url",
    "fileToken": "secret",
}


def pages(value=None, *, present=True, field_type="multi-file"):
    data = responses()
    for i in (0, 2):
        data[i]["list"][1]["field_type"] = field_type
        data[i]["list"][2]["field_type"] = "multi-file"
    for i in (1, 3):
        data[i]["work_item_fields"] = (
            [{"key": "progress", "name": "Files", "value": value}] if present else []
        )
    return data


def test_inventory_keeps_unobserved_distinct_and_never_exposes_references():
    fake = Client(pages([FILE]))
    result = AttachmentsReader(fake).collect(DEST, end=1)
    listed = next(i for i in result["items"] if i["state"] == "listed")
    assert listed["size_display"] == "67.6MB" and not listed["downloaded"]
    assert listed["name"] == FILE["name"] and listed["has_source_reference"]
    assert any(i["state"] == "unobserved" for i in result["items"])
    assert result["end_time_ms"] is None and not result["content_fetched"]
    assert "private-url" not in json.dumps(result) and "secret" not in json.dumps(
        result
    )
    assert len(fake.calls) == 4 and result["attachment_count"] == 1


@pytest.mark.parametrize("value,state", [(None, "empty"), ([], "empty")])
def test_explicit_empty(value, state):
    result = AttachmentsReader(Client(pages(value))).collect(DEST, end=1)
    assert (
        next(i for i in result["items"] if i["field_key"] == "progress")["state"]
        == state
    )


@pytest.mark.parametrize(
    "value",
    [{}, "opaque", [None], [FILE, FILE], [FILE | {"size": 123}], [FILE | {"name": ""}]],
)
def test_unknown_or_duplicate_members_fail_closed(value):
    with pytest.raises(ProjectReadError):
        AttachmentsReader(Client(pages(value))).collect(DEST, end=1)


def test_legacy_file_not_silently_interpreted_as_multifile():
    result = AttachmentsReader(Client(pages(FILE, field_type="file"))).collect(
        DEST, end=1
    )
    assert any(i["state"] == "unsupported_legacy_format" for i in result["items"])
    assert result["attachment_count"] == 0


def test_changed_source_rejects_inventory():
    data = pages([FILE])
    data[-1]["work_item_fields"][0]["value"] = [FILE | {"url": "changed"}]
    with pytest.raises(ProjectReadError, match="snapshot_changed_during_read"):
        AttachmentsReader(Client(data)).collect(DEST, end=1)


def test_durable_authorized_inventory(conn, config, context):
    args = context | {"kind": "attachments"}
    original = dict(
        conn.execute(
            "SELECT * FROM project_bugs WHERE bug_id=?", (args["bug_id"],)
        ).fetchone()
    )
    queued = activity.enqueue(conn, config, **args)
    result = activity.run_one(
        conn,
        lambda: config,
        client_factory=lambda _: Client(pages([copy.deepcopy(FILE)])),
    )
    assert result["state"] == "succeeded"
    assert (
        activity.enqueue(conn, config, **args)["activity_id"] == queued["activity_id"]
    )
    page = activity.page(
        conn, actor=args["actor"], activity_id=queued["activity_id"], offset=0
    )
    assert page["observation"]["attachment_count"] == 1
    assert (
        dict(
            conn.execute(
                "SELECT * FROM project_bugs WHERE bug_id=?", (args["bug_id"],)
            ).fetchone()
        )
        == original
    )
    with pytest.raises(ValueError):
        activity.page(conn, actor="other", activity_id=queued["activity_id"], offset=0)
