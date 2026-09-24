import json
import pytest

from test_conversation_context import admitted, case, item, reply

from k3_support import conversation_context as context
from k3_support.ids import canonical_json, digest


@pytest.mark.parametrize('old_policy', ['conversation-facts-v1', 'conversation-facts-v2'])
def test_old_policy_blocks_and_upgrade_invalidates_old_reply(conn, config, old_policy):
    key, snapshot = admitted(conn, config, item(content="当前 U-Boot 版本是 2025.01"))
    cid = case(conn, key)
    queued = reply(conn, config, key, cid)
    snapshot = context.resolve_event_context(conn, key)
    old_binding = snapshot["binding"]
    old_facts = json.loads(
        conn.execute(
            "SELECT facts_json FROM conversation_contexts WHERE context_id=?",
            (snapshot["context_id"],),
        ).fetchone()[0]
    )
    old_facts["policy"] = old_policy
    for mention in old_facts['mentions']:
        mention.pop('version_component', None)
    conn.execute(
        "UPDATE conversation_contexts SET facts_json=?,facts_digest=? WHERE context_id=?",
        (canonical_json(old_facts), digest(old_facts), snapshot["context_id"]),
    )
    before = list(conn.iterdump())
    assert context.context_snapshot(conn, snapshot['context_id'])['state'] == 'conflict'
    assert context.validate_context_binding(conn, old_binding) == (
        False,
        "context_policy_changed",
    )
    assert list(conn.iterdump()) == before
    upgraded = context.project_context(conn, snapshot["context_id"])
    versions = [m for m in upgraded['facts']['mentions'] if m['field'] == 'software_version']
    assert versions[0]['version_component'] == 'u-boot'
    assert versions[0]['source']['event_pk'] == key
    assert upgraded["revision"] == snapshot["revision"] + 1
    assert context.validate_context_binding(conn, upgraded["binding"])[0]
    assert context.validate_context_binding(conn, old_binding) == (
        False,
        "context_revision_changed",
    )
    assert (
        conn.execute(
            "SELECT state FROM outbox WHERE outbox_id=?", (queued["outbox_id"],)
        ).fetchone()[0]
        == "cancelled"
    )
    repeated = context.project_context(conn, snapshot["context_id"])
    assert repeated["binding"] == upgraded["binding"]
    assert conn.execute('SELECT count(*) FROM outbox').fetchone()[0] == 1
