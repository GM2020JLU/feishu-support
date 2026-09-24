"""Trusted append-only official CLI adapter, never a public model tool.

No successful native comment creation has been accepted yet. The narrow ACK
parser is provisional: any other shape stays unknown. A returned ID alone never
settles an operation; a separate identity-bound read must find its exact content.
No dispatcher is installed or enabled by importing this module.
"""

import json
import re

from . import project_bug_operations as operations
from .db import transaction
from .ids import canonical_json, digest
from .project_comments_reader import CommentsReader
from .project_read_client import MeegleReadClient, ProjectReadError
from .project_read_snapshot import SnapshotReader
from .project_transport import ProjectReceipt, ProjectView
from .timeutil import iso_now, parse_iso, utc_now


class CommentClient(MeegleReadClient):
    """Separate from the read-only client: exactly one create-comment command."""

    def create_comment(self, destination, text):
        if (
            not isinstance(destination, dict)
            or set(destination) != {"host", "project_key", "type_key", "item_id"}
            or destination["host"] != self.host
            or not all(
                isinstance(v, str) and v and len(v) <= 2048
                for v in destination.values()
            )
            or not re.fullmatch(r"[1-9][0-9]*", destination["item_id"])
        ):
            raise ProjectReadError("invalid_comment_destination")
        operations._change("bug.comment", {"text": text})
        # No retry loop, update mode, attachments, mentions transformation or
        # caller-controlled command flags. Official CLI internally retries only
        # HTTP 401 after token refresh; timeouts/5xx are not replayed by us.
        return self._success(
            [
                "comment",
                "add",
                "--params",
                json.dumps(
                    {
                        "project_key": destination["project_key"],
                        "work_item_id": destination["item_id"],
                        "action": "create",
                        "content": text,
                    },
                    ensure_ascii=False,
                ),
            ]
        )


def _ack(value):
    # Exactly the acknowledgement observed in the 2026-09-17 authorized
    # acceptance: {"action": "create", "success": true} without an identifier.
    # The comment id is recovered on reconciliation from the baseline diff.
    return value == {"action": "create", "success": True}


class CommentTransport:
    def __init__(
        self,
        conn,
        client,
        *,
        reader_digest,
        before_read,
        create_contract_verified=False,
    ):
        if type(create_contract_verified) is not bool:
            raise TypeError("explicit create contract verification flag required")
        self.create_contract_verified = create_contract_verified
        self.conn, self.client = conn, client
        if not isinstance(reader_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", reader_digest
        ):
            raise ValueError("trusted reader fingerprint required")
        if not callable(before_read):
            raise TypeError("read authorization guard required")
        self.reader_digest, self.before_read = reader_digest, before_read
        self.baseline = self.destination = self.observed_at = None

    def preflight(self, destination):
        observation = SnapshotReader(self.client, before_read=self.before_read).collect(
            destination
        )
        comments = CommentsReader(self.client, before_read=self.before_read).collect(
            destination, end=int(utc_now().timestamp() * 1000)
        )
        self.before_read()
        self.baseline = sorted(item["comment_id"] for item in comments["items"])
        self.destination, self.observed_at = dict(destination), observation["observed_at"]
        return ProjectView(
            destination=dict(destination),
            snapshot=observation["snapshot"],
            observed_at=self.observed_at,
            server_enforced_actions=(
                frozenset({"bug.comment"})
                if self.create_contract_verified
                else frozenset()
            ),
        )

    def _bound(self, packet):
        if not isinstance(packet, dict) or packet.get("action") != "bug.comment":
            raise ValueError("only dispatched comments are supported")
        op = operations._operation(self.conn, packet.get("operation_id"))
        if (
            op["state"] not in {"dispatched", "unknown"}
            or op["action"] != "bug.comment"
            or op["write_json"] != canonical_json(packet)
            or op["write_digest"] != digest(packet)
        ):
            raise ValueError("immutable dispatched packet required")
        return op

    def write(self, packet):
        if not self.create_contract_verified:
            raise PermissionError(
                "native comment creation contract has not been accepted"
            )
        with transaction(self.conn):
            op = self._bound(packet)
            if (
                op["state"] != "dispatched"
                or self.baseline is None
                or packet["destination"] != self.destination
                or not 0
                <= (utc_now() - parse_iso(self.observed_at)).total_seconds()
                <= 60
            ):
                raise ValueError("fresh preflight required")
            if self.conn.execute(
                "SELECT 1 FROM project_comment_attempts WHERE operation_id=?",
                (op["operation_id"],),
            ).fetchone():
                raise ValueError("comment was already attempted; reconcile only")
            self.conn.execute(
                "INSERT INTO project_comment_attempts(operation_id,write_digest,reader_digest,user_key,baseline_json,state,created_at,updated_at) VALUES(?,?,?,?,?,'reserved',?,?)",
                (
                    op["operation_id"],
                    op["write_digest"],
                    self.reader_digest,
                    self.client.user_key,
                    canonical_json(self.baseline),
                    iso_now(),
                    iso_now(),
                ),
            )
        try:
            response = self.client.create_comment(
                packet["destination"], packet["change"]["text"]
            )
            acknowledged = _ack(response)
            response_digest = digest(response)
        except Exception:  # noqa: BLE001 -- never save raw provider errors or replay writes
            acknowledged = False
            response_digest = None
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE project_comment_attempts SET state=?,comment_id=NULL,response_digest=?,updated_at=? WHERE operation_id=? AND state='reserved'",
                (
                    "acknowledged" if acknowledged else "unknown",
                    response_digest,
                    iso_now(),
                    op["operation_id"],
                ),
            )

    def reconcile(self, packet):
        self._bound(packet)
        unknown = ProjectReceipt(
            packet["operation_id"], digest(packet), "unknown", False
        )
        row = self.conn.execute(
            "SELECT * FROM project_comment_attempts WHERE operation_id=?",
            (packet["operation_id"],),
        ).fetchone()
        if (
            row is None
            or row["write_digest"] != digest(packet)
            or row["reader_digest"] != self.reader_digest
            or row["state"] != "acknowledged"
        ):
            return unknown
        # Verify canonical space/type/item before reading comments. A readable
        # list endpoint alone does not establish the work-item type identity.
        SnapshotReader(self.client, before_read=self.before_read).collect(
            packet["destination"]
        )
        comments = CommentsReader(self.client, before_read=self.before_read).collect(
            packet["destination"], end=int(utc_now().timestamp() * 1000)
        )
        self.before_read()
        # The accepted acknowledgement carries no identifier, so attribution
        # requires exactly one new comment from the identity that issued the
        # attempt (recorded immutably at write time, so a read-only client can
        # settle it) whose content matches the packet text (the server appends
        # one trailing newline on readback).
        baseline = set(json.loads(row["baseline_json"]))
        text = packet["change"]["text"]
        matches = [
            item
            for item in comments["items"]
            if item["comment_id"] not in baseline
            and item["creator"] == row["user_key"]
            and item["content"] in (text, text + "\n")
            and not item["attachment_reference"]
        ]
        if len(matches) != 1:
            return unknown
        return ProjectReceipt(
            packet["operation_id"],
            digest(packet),
            "applied",
            True,
            "project-comment:" + matches[0]["comment_id"],
        )
