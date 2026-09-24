"""Bounded, target-independent reference extraction; never deletion authority.

Callers must retain an incomplete row and block index readiness on any error.
Digests deliberately overapproximate references: collisions protect both targets.
"""

import hashlib
import json
import time


VERSION = 'retention-reference-strings-v1'


class ExtractionIncomplete(ValueError):
    pass


def reference_digest(value: str) -> str:
    if not isinstance(value, str):
        raise ExtractionIncomplete('reference is not a string')
    # Preserve escaped lone surrogates, too: they must not hide other strings.
    return hashlib.sha256(value.encode('utf-8', errors='surrogatepass')).hexdigest()


def extract_json(value, *, max_bytes=1024 * 1024, max_nodes=100_000,
                 max_seconds=1.0) -> frozenset[str]:
    """Hash every string value/key, including duplicate keys and future IDs.

    SQL NULL has no references. JSON decoding is bounded by input bytes; the
    traversal additionally has node/time limits. No partial result is returned.
    """
    if (type(max_bytes) is not int or max_bytes < 1
            or type(max_nodes) is not int or max_nodes < 1
            or type(max_seconds) not in (int, float)
            or not 0 < max_seconds <= 60):
        raise ValueError('invalid extraction budget')
    if value is None:
        return frozenset()
    if not isinstance(value, str):
        raise ExtractionIncomplete('JSON source is not text')
    if len(value) > max_bytes or len(value.encode('utf-8', errors='surrogatepass')) > max_bytes:
        raise ExtractionIncomplete('JSON byte budget exceeded')
    deadline = time.monotonic() + max_seconds
    def reject_constant(_):
        raise ValueError('non-JSON numeric constant')
    try:
        root = json.loads(value, object_pairs_hook=lambda pairs: pairs,
                          parse_constant=reject_constant)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ExtractionIncomplete('unreadable JSON') from exc
    pending, references, count = [root], set(), 0
    while pending:
        count += 1
        if count > max_nodes or time.monotonic() >= deadline:
            raise ExtractionIncomplete('JSON traversal budget exceeded')
        item = pending.pop()
        if isinstance(item, str):
            references.add(reference_digest(item))
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return frozenset(references)
