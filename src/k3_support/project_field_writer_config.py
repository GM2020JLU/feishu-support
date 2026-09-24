"""Server-owned writer identity, field whitelist and release-accepted contracts."""

from .project_read_client import _string

# Native update/transition contracts were accepted on 2026-09-17 against the
# official npm 1.0.23 Linux x64 binary (authorized field update, state
# transition and readback on the dedicated K3 test defect 7117732933). Never
# configurable from a browser or worker.
ACCEPTED_UPDATE_CLIENTS = frozenset(
    {"ffdc86254fe40962efc1b83b7c4f098694b76d9ba92bf92eaef46cf0f5d78362"}
)
ACCEPTED_TRANSITION_CLIENTS = frozenset(
    {"ffdc86254fe40962efc1b83b7c4f098694b76d9ba92bf92eaef46cf0f5d78362"}
)

# forbid: previews, already-matches settlement and manual application only.
# recheck_window: dispatch after re-reading the item's update marker; this is an
# explicitly configured risk decision, never a server CAS or atomicity claim.
RISK_POLICIES = {"forbid", "recheck_window"}


def validate(value):
    if (
        not isinstance(value, dict)
        or set(value) - {"closing_status_ids"}
        != {"enabled", "user_key", "allowed_fields", "risk_policy"}
        or type(value["enabled"]) is not bool
        or not _string(value["user_key"])
        or not isinstance(value["allowed_fields"], list)
        or not value["allowed_fields"]
        or len(value["allowed_fields"]) > 100
        or len(set(value["allowed_fields"])) != len(value["allowed_fields"])
        or not all(_string(key) for key in value["allowed_fields"])
        or value["risk_policy"] not in RISK_POLICIES
    ):
        raise ValueError(
            "field writer needs a dedicated identity, an explicit field whitelist"
            " and an explicit risk policy"
        )
    closing = value.get("closing_status_ids", [])
    if (
        not isinstance(closing, list)
        or len(closing) > 20
        or len(set(closing)) != len(closing)
        or not all(_string(key) for key in closing)
    ):
        raise ValueError("closing status ids must be an explicit unique list")
    return dict(value) | {"closing_status_ids": list(closing)}


def accepted_update(reader):
    return reader["sha256"] in ACCEPTED_UPDATE_CLIENTS


def accepted_transition(reader):
    return reader["sha256"] in ACCEPTED_TRANSITION_CLIENTS
