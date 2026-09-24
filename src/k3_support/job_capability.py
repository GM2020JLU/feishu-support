"""Shared secret/context check only; callers must separately verify live authority."""

import hashlib
import hmac
import json
import re


class CapabilityError(ValueError):
    pass


def verified_context(raw_context, capability):
    try:
        context = json.loads(raw_context)
    except (TypeError, ValueError, RecursionError) as error:
        raise CapabilityError("Codex job context is invalid") from error
    if not isinstance(context, dict):
        raise CapabilityError("Codex job context is invalid")
    expected = context.get("capability_sha256")
    if (not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected)
            or not isinstance(capability, str) or not capability or len(capability) > 4096):
        raise CapabilityError("Codex job capability mismatch")
    try:
        actual = hashlib.sha256(capability.encode()).hexdigest()
    except UnicodeError as error:
        raise CapabilityError("Codex job capability mismatch") from error
    if not hmac.compare_digest(actual, expected):
        raise CapabilityError("Codex job capability mismatch")
    return context
