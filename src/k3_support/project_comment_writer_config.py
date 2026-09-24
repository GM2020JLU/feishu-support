"""Server-owned writer identity and release-accepted native creation contracts."""

from .project_read_client import _string

# The native comment-create contract was accepted on 2026-09-17 against the
# official npm 1.0.23 Linux x64 binary: `comment add` acknowledged with
# {"action": "create", "success": true} and the comment readable with its id
# on the dedicated K3 test defect 7117732933. Never configurable from a
# browser or worker.
ACCEPTED_CREATE_CLIENTS = frozenset(
    {"ffdc86254fe40962efc1b83b7c4f098694b76d9ba92bf92eaef46cf0f5d78362"}
)


def validate(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"enabled", "user_key"}
        or type(value["enabled"]) is not bool
        or not _string(value["user_key"])
    ):
        raise ValueError("comment writer needs an explicit dedicated user identity")
    return dict(value)


def accepted(reader):
    return reader["sha256"] in ACCEPTED_CREATE_CLIENTS
