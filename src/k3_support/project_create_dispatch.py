"""Synchronous native work-item creation with draft custody, controls-only.

The native create contract was accepted on 2026-09-17 against the official npm
1.0.23 Linux x64 binary: `workitem create` takes an array of field objects
whose field_value is always a string (complex values canonically JSON-encoded)
and acknowledges with {"url": ..., "work_item_id": <int>} where the URL binds
the space, type and new item id. Anything else is not an acknowledgement.

Creation is dispatched synchronously inside draft custody: reserve first, one
native call, then settle as created or unknown. A crash between reserve and
settlement leaves the draft dispatched for explicit human settlement; nothing
is ever retried automatically.
"""

import json

from . import project_bug_create as drafts
from . import (
    project_comment_writer_config,
    project_create_duplicates,
    project_create_schema,
)
from .ids import canonical_json, digest
from .project_read_client import MeegleReadClient, ProjectReadError, parse_json
from .project_reader_config import selected

# Accepted 2026-09-17 (dedicated K3 test defect 7117732933 created and read
# back through the pinned CLI). Never configurable from a browser or worker.
ACCEPTED_WORKITEM_CREATE_CLIENTS = frozenset(
    {"ffdc86254fe40962efc1b83b7c4f098694b76d9ba92bf92eaef46cf0f5d78362"}
)


class CreateBlocked(ValueError):
    pass


class CreateRejected(Exception):
    """Reserved for a transport with verified no-side-effect rejection semantics."""


def accepted(reader):
    return reader["sha256"] in ACCEPTED_WORKITEM_CREATE_CLIENTS


class CreateClient(MeegleReadClient):
    """Separate from the read-only client: exactly one creation command."""

    def _success(self, args):
        # A CLI error envelope describes an error, not whether the server
        # committed a work item. Keep unproven outcomes unknown so custody and
        # creation budget cannot be released merely on SERVER_CALL_FAILED.
        result = self._invoke(args)
        if result.returncode != 0:
            raise ProjectReadError("remote_read_failed")
        value = parse_json(result.stdout)
        if isinstance(value, dict) and value.get("error") is not None:
            raise ProjectReadError("remote_read_failed")
        return value

    @staticmethod
    def validate_intent(scope, host, field_values):
        if (
            not isinstance(scope, dict)
            or set(scope) != {"host", "project_key", "type_key"}
            or scope["host"] != host
            or not all(
                isinstance(v, str) and v and len(v) <= 2048 for v in scope.values()
            )
        ):
            raise ProjectReadError("invalid_create_scope")
        if not isinstance(field_values, dict) or not field_values:
            raise ProjectReadError("invalid_create_fields")

    def create_workitem(self, scope, field_values):
        self.validate_intent(scope, self.host, field_values)
        return self._success(
            [
                "workitem",
                "create",
                "--params",
                json.dumps(
                    {
                        "project_key": scope["project_key"],
                        "work_item_type": scope["type_key"],
                        "fields": [
                            {
                                "field_key": key,
                                "field_value": value
                                if isinstance(value, str)
                                else canonical_json(value),
                            }
                            for key, value in sorted(field_values.items())
                        ],
                    },
                    ensure_ascii=False,
                ),
            ]
        )


def _ack(scope, value):
    if (
        not isinstance(value, dict)
        or set(value) != {"url", "work_item_id"}
        or type(value["work_item_id"]) is not int
        or not 0 < value["work_item_id"] < 2**63
        or not isinstance(value["url"], str)
    ):
        return None
    item_id = str(value["work_item_id"])
    expected = "https://{}/{}/{}/detail/{}".format(
        scope["host"], scope["project_key"], scope["type_key"], item_id
    )
    return item_id if value["url"] == expected else None


def _identity(client, host, user_key):
    envelope = client.read_page("user.me", {})
    if (
        not isinstance(envelope, dict)
        or envelope.get("host") != host
        or envelope.get("command") != "user.me"
        or not isinstance(envelope.get("payload"), dict)
        or envelope["payload"].get("user_key") != user_key
    ):
        raise CreateBlocked("writer_identity_changed")


def send(conn, config, *, draft_id, actor, expected_digest, client_factory=None):
    """One reserved native creation attempt for a ready, owned draft."""
    project = config.raw.get("project_integration") or {}
    if project.get("write_enabled") is not True:
        raise CreateBlocked("write_provider_unavailable")
    writer = project.get("comment_writer")
    if writer is None:
        raise CreateBlocked("writer_not_configured")
    writer = project_comment_writer_config.validate(writer)
    if not writer["enabled"]:
        raise CreateBlocked("writer_not_configured")
    reader = selected(config)
    if reader is None:
        raise CreateBlocked("reader_unavailable")
    if not accepted(reader):
        raise CreateBlocked("native_contract_unverified")
    row = drafts._owned(conn, draft_id, actor)
    drafts._expect(row, expected_digest)
    drafts._require_grant(conn, row)
    if row["state"] != "ready":
        raise drafts.BugConflict("create draft is not ready")
    if reader["host"] != row["host"]:
        raise CreateBlocked("reader_destination_mismatch")
    scope = {
        "host": row["host"],
        "project_key": row["project_key"],
        "type_key": row["type_key"],
    }
    project_create_duplicates.require_current(conn, config, row)
    field_values = json.loads(row["field_values_json"])
    client = (
        client_factory
        or (lambda r: CreateClient(**{k: v for k, v in r.items() if k != "enabled"}))
    )(reader)
    # Everything that can fail without touching the remote item runs before the
    # draft's single dispatch is consumed: intent validation and the pinned
    # writer identity proof. After reserve, the only remaining failure source
    # is the native call itself, so custody settles created, rejected or
    # unknown.
    CreateClient.validate_intent(scope, reader["host"], field_values)
    _identity(client, reader["host"], writer["user_key"])
    metadata = project_create_schema.check(client, scope, field_values)
    from .project_create_related import validate as validate_related
    related_recheck = validate_related(client, config, scope, metadata, field_values)

    def before_reserve(current):
        project_create_duplicates.require_current(conn, config, current)
        related_recheck()

    drafts.reserve_dispatch(
        conn, draft_id=draft_id, actor=actor, expected_digest=expected_digest,
        before_reserve=before_reserve,
    )
    try:
        response = client.create_workitem(scope, field_values)
        item_id = _ack(scope, response)
    except CreateRejected:
        return drafts.settle_rejected(
            conn, draft_id=draft_id, actor=actor, error_code="provider_rejected"
        )
    except Exception:  # noqa: BLE001 -- never save raw provider errors or replay creates
        return drafts.settle_unknown(conn, draft_id=draft_id, actor=actor)
    if item_id is not None:
        return drafts.settle_created(
            conn,
            draft_id=draft_id,
            actor=actor,
            created_item_id=item_id,
            response_digest=digest(response),
        )
    return drafts.settle_unknown(conn, draft_id=draft_id, actor=actor)
