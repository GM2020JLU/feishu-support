# ruff: noqa: F811 -- shared isolated fixtures
import copy
import json
import sqlite3

import pytest
from test_project_activity import context  # noqa: F401
from test_project_read_client import AUTH, binary, client  # noqa: F401
from test_project_read_snapshot import DEST, Client, bound

from k3_support import db
from k3_support import project_activity as activity
from k3_support import project_bug_grants as grants
from k3_support.project_read_client import ProjectReadError
from k3_support.project_relations_reader import RelationsReader
from k3_support.runtime_control import ensure_global_state


def definition(key="role", disabled=False):
    return {
        "id": key,
        "name": "Found version role",
        "work_item_type_key": "issue",
        "work_item_type_name": "Bug",
        "disabled": disabled,
        "relation_type": 0,
        "relation_details": [
            {
                "project_key": "space",
                "project_name": "Space",
                "work_item_type_key": "version",
                "work_item_type_name": "Software release",
            }
        ],
    }


def metadata(*rows):
    return {"list": list(rows) if rows else [definition()]}


def relation_page(number=1, total=1):
    return {
        "list": [
            {
                "id": n + 1000,
                "name": "<script>version</script>",
                "project_key": "space",
                "work_item_type_key": "version",
            }
            for n in range((number - 1) * 50, min(number * 50, total))
        ]
        or None,
        "pagination": {"page_num": number, "page_size": 50, "total": total},
    }


def single():
    return [metadata(), relation_page(), metadata(), relation_page()]


def collect(values, hook=None):
    fake = Client(values, hook=hook)
    return RelationsReader(fake).collect(DEST, end=1800000000000), fake


def test_relations_preserve_official_roles_without_following_targets_or_claiming_cutoff():
    result, fake = collect(single())
    assert result["definition_count"] == result["target_count"] == 1
    row = result["items"][0]
    assert (
        row["relation_name"] == "Found version role"
        and row["target"]["name"] == "<script>version</script>"
    )
    assert (
        row["target"]["item_id"] == "1000"
        and row["target"]["type_name"] == "Software release"
    )
    assert (
        result["end_time_ms"] is None
        and not result["atomic_snapshot"]
        and not result["target_details_fetched"]
    )
    assert result["definition_scope"] == "returned_to_current_identity"
    assert [c[0] for c in fake.calls] == [
        "relation.meta-definitions",
        "relation.list",
        "relation.meta-definitions",
        "relation.list",
    ]
    assert all(
        c[1]["work_item_id"] == "123" for c in fake.calls if c[0] == "relation.list"
    )
    assert all("end" not in c[1] for c in fake.calls)


def test_empty_and_disabled_are_distinct_and_disabled_does_not_fetch():
    meta = metadata(definition("empty"), definition("disabled", True))
    result, fake = collect(
        [meta, relation_page(total=0), copy.deepcopy(meta), relation_page(total=0)]
    )
    assert {r["relation_id"]: r["state"] for r in result["items"]} == {
        "disabled": "disabled",
        "empty": "empty",
    }
    assert result["target_count"] == 0 and result["disabled_definitions"] == 1
    assert all(
        c[1]["relation_id"] == "empty" for c in fake.calls if c[0] == "relation.list"
    )


def test_relation_pagination_keeps_same_target_under_different_roles():
    meta = metadata(definition("a"), definition("b"))
    result, fake = collect(
        [
            meta,
            relation_page(total=51),
            relation_page(2, 51),
            relation_page(),
            copy.deepcopy(meta),
            relation_page(total=51),
            relation_page(),
        ]
    )
    assert result["target_count"] == 52 and result["pages"] == 3
    assert sum(i["target"]["item_id"] == "1000" for i in result["items"]) == 2
    assert len(fake.calls) == 7


@pytest.mark.parametrize(
    "change",
    [
        "source-type",
        "duplicate-definition",
        "bad-disabled",
        "missing-target",
        "duplicate-target-definition",
        "large-schema",
    ],
)
def test_bad_definitions_cannot_authorize_relation_queries(change):
    meta = metadata()
    if change == "source-type":
        meta["list"][0]["work_item_type_key"] = "another"
    if change == "duplicate-definition":
        meta["list"].append(copy.deepcopy(meta["list"][0]))
    if change == "bad-disabled":
        meta["list"][0]["disabled"] = 0
    if change == "missing-target":
        meta["list"][0]["relation_details"] = []
    if change == "duplicate-target-definition":
        meta["list"][0]["relation_details"] *= 2
    if change == "large-schema":
        meta["list"] = [definition(str(i)) for i in range(51)]
    fake = Client([meta])
    with pytest.raises(ProjectReadError):
        RelationsReader(fake).collect(DEST, end=1)
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        "project",
        "type",
        "boolean-id",
        "numeric-string",
        "empty-name",
        "missing-row",
        "wrong-page",
        "wrong-size",
        "bad-total",
        "duplicate",
    ],
)
def test_bad_target_pages_fail_closed(change):
    page = relation_page(total=2)
    if change == "project":
        page["list"][0]["project_key"] = "other"
    if change == "type":
        page["list"][0]["work_item_type_key"] = "other"
    if change == "boolean-id":
        page["list"][0]["id"] = True
    if change == "numeric-string":
        page["list"][0]["id"] = "1000"
    if change == "empty-name":
        page["list"][0]["name"] = ""
    if change == "missing-row":
        page["list"].pop()
    if change == "wrong-page":
        page["pagination"]["page_num"] = 2
    if change == "wrong-size":
        page["pagination"]["page_size"] = 20
    if change == "bad-total":
        page["pagination"]["total"] = True
    if change == "duplicate":
        page["list"][1] = copy.deepcopy(page["list"][0])
    with pytest.raises(ProjectReadError):
        collect([metadata(), page])


@pytest.mark.parametrize(
    "change",
    [
        "changed-schema",
        "changed-count",
        "changed-target",
        "cross-page-duplicate",
        "page-budget",
    ],
)
def test_changed_or_unbounded_graph_is_not_saved_as_complete(change):
    values = single()
    if change == "changed-schema":
        values[2]["list"][0]["name"] = "Changed role"
    if change == "changed-count":
        values[-1] = relation_page(total=2)
    if change == "changed-target":
        values[-1]["list"][0]["name"] = "Changed target"
    if change == "cross-page-duplicate":
        second = relation_page(2, 51)
        second["list"][0]["id"] = 1000
        values = [metadata(), relation_page(total=51), second]
    if change == "page-budget":
        values = [metadata()] + [relation_page(n, 1001) for n in range(1, 21)]
    with pytest.raises(ProjectReadError):
        collect(values)


def test_metadata_and_bookend_reordering_is_not_a_false_change():
    meta = metadata(definition("a"), definition("b"))
    reordered = copy.deepcopy(meta)
    reordered["list"].reverse()
    first = relation_page(total=2)
    last = copy.deepcopy(first)
    last["list"].reverse()
    result, _ = collect(
        [meta, first, relation_page(), reordered, last, relation_page()]
    )
    assert result["target_count"] == 3


def test_explicit_cross_space_relation_returns_only_edge_summary():
    meta = metadata()
    meta["list"][0]["relation_details"][0]["project_key"] = "other-space"
    page = relation_page()
    page["list"][0]["project_key"] = "other-space"
    result, fake = collect([meta, page, copy.deepcopy(meta), copy.deepcopy(page)])
    assert result["items"][0]["target"]["project_key"] == "other-space"
    assert all(c[1]["project_key"] == "space" for c in fake.calls)


def test_native_boundary_requires_a_verified_relation_id(binary):
    c, calls = client(binary, [(0, AUTH), (0, relation_page())])
    params = {
        "project_key": "space",
        "work_item_id": "123",
        "page_num": 1,
        "page_size": 50,
    }
    with pytest.raises(ProjectReadError):
        c.read_page("relation.list", params)
    assert calls == []
    c.read_page("relation.list", params | {"relation_id": "role"})
    assert len(calls) == 2


def test_durable_relation_queue_reuses_grant_and_persists_only_local_read_result(
    conn, config, context
):
    args = context | {"kind": "relations"}
    queued = activity.enqueue(conn, config, **args)
    assert activity.enqueue(conn, config, **args) == queued
    fake = Client(single())
    result = activity.run_one(conn, lambda: config, client_factory=lambda _: fake)
    assert result["state"] == "succeeded" and result["kind"] == "relations"
    page = activity.page(
        conn, actor="owner", activity_id=result["activity_id"], offset=0
    )
    assert page["items"][0]["target"]["item_id"] == "1000"
    for table in ("jobs", "project_bug_operations"):
        assert conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


def test_revocation_during_relation_bookends_discards_result(conn, config, context):
    activity.enqueue(conn, config, **(context | {"kind": "relations"}))

    def hook(n):
        if n == 4:
            grants.revoke(conn, grant_id=context["grant_id"], actor="owner")

    fake = Client(single(), hook=hook)
    result = activity.run_one(conn, lambda: config, client_factory=lambda _: fake)
    assert result["state"] == "blocked" and result["observation"] is None


@pytest.mark.parametrize("legacy_version", [122, 123, 124])
def test_migration_preserves_old_observation_order_grants_and_immutability(
    tmp_path, config, monkeypatch, legacy_version
):
    migrations = db.migration_files()
    legacy = db.connect(tmp_path / "legacy" / "support.db")
    with monkeypatch.context() as scoped:
        scoped.setattr(
            db, "migration_files", lambda: [m for m in migrations if m[0] <= legacy_version]
        )
        db.migrate(legacy)
    bug, args = bound.__wrapped__(legacy)
    config.raw["identity"]["control_operator_id"] = "owner"
    from test_project_refresh import READER

    config.raw["project_integration"] = {
        "write_enabled": False,
        "reader": copy.deepcopy(READER),
    }
    ensure_global_state(
        legacy, actor_id="owner", source="test", external_id="migration"
    )
    queued = activity.enqueue(
        legacy,
        config,
        actor="owner",
        bug_id=bug["bug_id"],
        grant_id=args["grant_id"],
        request_id="before",
        kind="comments",
    )
    result = {"items": [], "record_count": 0}
    legacy.execute(
        "UPDATE project_activity_requests SET rowid=77,state='succeeded',result_json=?",
        (json.dumps(result),),
    )
    before = dict(
        legacy.execute("SELECT rowid,* FROM project_activity_requests").fetchone()
    )
    assert db.migrate(legacy) == [m[0] for m in migrations if m[0] > legacy_version]
    assert (
        dict(legacy.execute("SELECT rowid,* FROM project_activity_requests").fetchone())
        == before | {"source_json": None}
    )
    assert activity.status(legacy, actor="owner", activity_id=queued["activity_id"])[
        "observation"
    ] == {"record_count": 0}
    with pytest.raises(sqlite3.IntegrityError):
        legacy.execute("UPDATE project_activity_requests SET result_json='{}'")
    with pytest.raises(sqlite3.IntegrityError):
        legacy.execute("DELETE FROM project_activity_requests")
    added = activity.enqueue(
        legacy,
        config,
        actor="owner",
        bug_id=bug["bug_id"],
        grant_id=args["grant_id"],
        request_id="after",
        kind="relations",
    )
    assert added["kind"] == "relations" and db.integrity(legacy)["ok"]
    assert db.migrate(legacy) == []
    legacy.close()
