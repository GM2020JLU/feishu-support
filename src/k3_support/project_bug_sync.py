"""Control-owned single-Bug refresh; the client/config is never worker input.

No creation, scheduler, public HTTP route or credential management in this module.
Each request checks scope, and persistence rechecks scope atomically with the CAS.
"""

import json

from . import project_bug_grants as grants
from . import project_bugs as bugs
from .project_read_snapshot import SnapshotReader


def refresh(conn, client, *, bug_id, actor, grant_id, observation_id, guard=None):
    """guard is a trusted consumer's lease/config fence, never request data."""
    bugs._text(observation_id, "observation ID")
    bug = dict(bugs._bug(conn, bug_id))
    if guard is not None:
        guard()
    grants.require_bug_read(conn, bug, actor=actor, grant_id=grant_id)
    old = conn.execute(
        "SELECT s.*, e.actor,e.grant_id,e.evidence_json FROM project_bug_snapshots s "
        "LEFT JOIN project_bug_read_evidence e USING(snapshot_id) "
        "WHERE s.bug_id=? AND s.observation_id=?",
        (bug_id, observation_id),
    ).fetchone()
    if old:
        if old["actor"] != actor or old["grant_id"] != grant_id:
            raise bugs.BugConflict("observation ID belongs to another read intent")
        return {
            "snapshot_id": old["snapshot_id"],
            "sequence": old["sequence"],
            "observed_at": old["observed_at"],
            "read_evidence": json.loads(old["evidence_json"]),
        }
    destination = {k: bug[k] for k in ("host", "project_key", "type_key", "item_id")}

    def before_read():
        if guard is not None:
            guard()
        grants.require_bug_read(conn, bug, actor=actor, grant_id=grant_id)

    reader = SnapshotReader(client, before_read=before_read)
    bundle = reader.collect(destination)
    evidence = {"read_started_at": bundle["read_started_at"], **bundle["read_evidence"]}
    row = bugs.observe(
        conn,
        bug_id=bug_id,
        observation_id=observation_id,
        expected_sequence=bug["snapshot_sequence"],
        payload=bundle["snapshot"],
        observed_at=bundle["observed_at"],
        read_source={"actor": actor, "grant_id": grant_id, "evidence": evidence},
        before_record=guard,
    )
    return {
        "snapshot_id": row["snapshot_id"],
        "sequence": row["sequence"],
        "observed_at": row["observed_at"],
        "read_evidence": evidence,
    }
