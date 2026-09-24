"""Field attachment inventory from a bounded, identity-checked source snapshot.

No signed URLs, opaque file references or token-bearing objects enter this public
projection. Inventory is not a download receipt or permission to execute content.
"""

from .ids import digest
from .project_read_client import ProjectReadError
from .project_read_snapshot import SnapshotReader


def inventory(observation):
    fields = observation["snapshot"]["fields"]
    evidence = observation["read_evidence"]
    # Defined-but-unset fields appear in the snapshot as None (they are
    # backfill targets); the read evidence is what actually distinguishes
    # never-observed from confirmed-empty for download decisions.
    unobserved = set(evidence.get("unobserved_field_keys") or ())
    items, count = [], 0
    for key, definition in sorted(evidence["attachment_fields"].items()):
        base = {
            "field_key": key,
            "field_name": definition["name"],
            "field_type": definition["type"],
        }
        if key not in fields or key in unobserved:
            items.append(base | {"state": "unobserved"})
            continue
        value = fields[key]
        if value is None or value == []:
            items.append(base | {"state": "empty"})
            continue
        if definition["type"] == "file":
            items.append(base | {"state": "unsupported_legacy_format"})
            continue
        if not isinstance(value, list) or len(value) > 1000:
            raise ProjectReadError("invalid_attachment_response")
        seen = set()
        for position, member in enumerate(value):
            if not isinstance(member, dict) or not all(
                isinstance(member.get(k), str) and 0 < len(member[k]) <= 4096
                for k in ("name", "size", "type")
            ):
                raise ProjectReadError("invalid_attachment_response")
            identity = digest(member)
            if identity in seen:
                raise ProjectReadError("duplicate_attachment_reference")
            seen.add(identity)
            count += 1
            if count > 10000:
                raise ProjectReadError("attachment_budget_exceeded")
            items.append(
                base
                | {
                    "state": "listed",
                    "position": position,
                    "source_digest": identity,
                    "name": member["name"],
                    "size_display": member["size"],
                    "media_type_display": member["type"],
                    "has_source_reference": isinstance(member.get("url"), str)
                    and bool(member["url"]),
                    "downloaded": False,
                }
            )
    if len(items) > 10000:
        raise ProjectReadError("attachment_budget_exceeded")
    return {
        "destination": observation["destination"],
        "observed_at": observation["observed_at"],
        "read_started_at": observation["read_started_at"],
        "end_time_ms": None,
        "items": items,
        "attachment_count": count,
        "field_count": len(evidence["attachment_fields"]),
        "source_digest": digest(observation["snapshot"]),
        "scope": "attachment_fields_only",
        "pagination_complete": evidence["pagination_complete"],
        "bookends_equal": evidence["bookends_equal"],
        "atomic_snapshot": False,
        "content_fetched": False,
        "remote_requests": evidence["remote_requests"],
    }


class AttachmentsReader:
    def __init__(self, client, *, before_read=None):
        self.reader = SnapshotReader(client, before_read=before_read)

    def collect(self, destination, *, end):
        # No attachment endpoint cutoff exists; never imply end-time isolation.
        return inventory(self.reader.collect(destination))
