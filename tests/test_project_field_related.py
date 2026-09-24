"""Related-version edits keep target reads scoped and final writes conflict checked."""

# ruff: noqa: F811
import copy
from datetime import timedelta

import pytest
from test_project_bug_operations import setup  # noqa: F401
from test_project_read_snapshot import Client
from test_project_refresh import READER
from test_project_write_transport import (
    WriteFake,
    custody,
    history,
    make_transport,
    op_record,
    snapshot_pages,
)

from k3_support import project_bug_grants as grants
from k3_support import project_bug_operations as ops
from k3_support import project_bugs as bugs
from k3_support.project_field_related import ids, search, validate_values
from k3_support.project_read_snapshot import SnapshotReader
from k3_support.project_write_transport import _field_confirmed
from k3_support.timeutil import iso_now, utc_now

KIND = "workitem_related_multi_select"
READ_SCOPE = {
    "simple_name": "space",
    "project_key": "space",
    "type_key": "version",
    "allowed_item_ids": [100, 200],
}
BASE = [{"id": 100, "name": "Old version"}]


def relation_pages(value=None, marker="opaque-marker"):
    pages = snapshot_pages(BASE if value is None else value, marker)
    for index in (0, 2):
        for field in pages[index]["list"]:
            if field["field_key"] == "progress":
                field["field_type"] = KIND
    return pages


class RelatedFake(WriteFake):
    def __init__(self, pages=(), *, missing=False, hook=None):
        super().__init__(list(pages))
        self.queries = []
        self.missing, self.target_hook = missing, hook

    def read_page(self, command, params):
        if command == "workitem.meta-fields" and "field_keys" in params:
            return {
                "host": self.host,
                "command": command,
                "payload": {
                    "list": [
                        {
                            "field_key": "progress",
                            "field_type": KIND,
                            "related_work_item_info": [
                                {"project_key": "space", "work_item_type": "version"}
                            ],
                        }
                    ],
                    "pagination": {
                        "page_num": 1,
                        "page_size": 50,
                        "has_more": False,
                        "total": 1,
                    },
                },
            }
        return super().read_page(command, params)

    def query_bugs(self, scope, **kwargs):
        self.queries.append((copy.deepcopy(scope), kwargs))
        if self.target_hook:
            self.target_hook()
        return {
            "host": self.host,
            "next_after_id": None,
            "items": [
                {"item_id": str(i), "title": "Version " + str(i)}
                for i in ([] if self.missing else scope["allowed_item_ids"])
            ],
        }


def ready(conn, setup, *, missing=False):
    cfg, request, adapter = setup
    cfg.raw["project_integration"].update(
        reader=dict(READER), search_spaces=[copy.deepcopy(READ_SCOPE)]
    )
    bug = bugs.detail(conn, request["bug_id"])
    grant = grants.issue(
        conn,
        actor="owner",
        request_id="related-fields",
        expires_at=(utc_now() + timedelta(hours=1)).isoformat(),
        scope={
            "host": bug["host"],
            "project_key": "space",
            "type_key": "type",
            "bug_ids": [bug["bug_id"]],
            "actions": ["bug.read", "bug.fields"],
            "fields": ["progress"],
            "transitions": [],
            "repositories": [],
            "devices": [],
        },
    )
    bundle = SnapshotReader(Client(relation_pages())).collect(adapter.view.destination)
    if missing:
        bundle["read_evidence"]["unobserved_field_keys"].append("progress")
    observation = bugs.observe(
        conn,
        bug_id=bug["bug_id"],
        observation_id="related-source",
        expected_sequence=1,
        payload=bundle["snapshot"],
        observed_at=iso_now(),
        read_source={
            "actor": "owner",
            "grant_id": grant["grant_id"],
            "evidence": bundle["read_evidence"],
        },
    )
    args = {
        "actor": "owner",
        "bug_id": bug["bug_id"],
        "grant_id": grant["grant_id"],
        "field_key": "progress",
        "query": "version",
    }
    request.update(
        grant_id=grant["grant_id"],
        snapshot_id=observation["snapshot_id"],
        expected_revision=bugs.detail(conn, bug["bug_id"])["revision"],
        change={"fields": {"progress": [200]}},
    )
    return cfg, args, request, adapter.view.destination


def test_search_observed_relation_and_fences_revocation(conn, setup):
    cfg, args, _, _ = ready(conn, setup)
    client = RelatedFake()
    assert search(conn, cfg, **args, client_factory=lambda _: client)["options"] == [
        {"value": "100", "label": "Version 100"},
        {"value": "200", "label": "Version 200"},
    ]
    assert client.queries == [(READ_SCOPE, {"keyword": "version"})]
    client = RelatedFake(
        hook=lambda: grants.revoke(conn, grant_id=args["grant_id"], actor="owner")
    )
    with pytest.raises(PermissionError):
        search(conn, cfg, **args, client_factory=lambda _: client)


def test_search_rejects_unobserved_baseline_without_network(conn, setup):
    cfg, args, _, _ = ready(conn, setup, missing=True)
    client = RelatedFake()
    with pytest.raises(ValueError, match="observed"):
        search(conn, cfg, **args, client_factory=lambda _: client)
    assert not client.calls and not client.queries


def test_relation_validation_requires_scope_and_resolving_ids(conn, setup):
    cfg, _, _, destination = ready(conn, setup)
    evidence = {"field_types": {"progress": KIND}}
    client = RelatedFake()
    with pytest.raises(PermissionError):
        validate_values(
            client, cfg, destination, evidence, {"progress": [300]}, lambda: None
        )
    assert not client.queries
    with pytest.raises(ValueError, match="resolve"):
        validate_values(
            RelatedFake(missing=True),
            cfg,
            destination,
            evidence,
            {"progress": [200]},
            lambda: None,
        )
    cfg.raw["project_integration"]["search_spaces"] = []
    with pytest.raises(PermissionError):
        validate_values(
            client, cfg, destination, evidence, {"progress": []}, lambda: None
        )


@pytest.mark.parametrize("value", [[True], [0], [2**53], [100, 100], ["100"], "100"])
def test_bad_proposed_ids_rejected(value):
    with pytest.raises(ValueError):
        ids(KIND, value)


def test_readback_uses_ids_not_labels_and_rejects_invented_shapes():
    assert _field_confirmed(
        [{"id": 200, "name": "renamed"}, {"id": 100, "name": "same"}], [100, 200], KIND
    )
    assert not _field_confirmed([{"id": 300, "name": "same"}], [100], KIND)
    assert not _field_confirmed([{"id": "100", "name": "same"}], [100], KIND)
    assert not _field_confirmed(
        [{"id": 100, "name": "same", "unknown": 1}], [100], KIND
    )
    assert _field_confirmed(None, [], KIND)


@pytest.mark.parametrize("failure", ["missing", "conflict", "none"])
def test_real_dispatch_path_checks_targets_before_final_snapshot(conn, setup, failure):
    cfg, _, request, _ = ready(conn, setup)
    operation = ops.prepare(conn, **request)
    changed = [{"id": 200, "name": "New version"}]
    final = (
        relation_pages(marker="colleague-edited")
        if failure == "conflict"
        else relation_pages()
    )
    pages = relation_pages() + final + relation_pages(changed) + history(op_record())
    client = RelatedFake(pages, missing=failure == "missing")
    result = ops.dispatch(
        conn,
        cfg,
        operation_id=operation["operation_id"],
        transport=make_transport(conn, client, config=cfg),
    )
    assert client.queries == [(READ_SCOPE | {"allowed_item_ids": [200]}, {})]
    if failure == "none":
        assert result["state"] == "confirmed"
        assert len(client.updates) == 1
    else:
        assert not client.updates
        assert custody(conn)[0]["state"] == (
            "not_issued" if failure == "missing" else "precondition_failed"
        )
