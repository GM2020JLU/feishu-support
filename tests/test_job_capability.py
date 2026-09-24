import hashlib
import json

import pytest

from k3_support.job_capability import CapabilityError, verified_context


def test_verified_context_preserves_additional_binding_fields():
    context = {"capability_sha256": hashlib.sha256(b"synthetic").hexdigest(), "board_session_id": "session-1"}
    assert verified_context(json.dumps(context), "synthetic") == context


@pytest.mark.parametrize("expected", [None, [], {}, 123, "a" * 63, "G" * 64, "a" * 65])
def test_malformed_digest_rejected(expected):
    with pytest.raises(CapabilityError, match="mismatch"):
        verified_context(json.dumps({"capability_sha256": expected}), "synthetic")


@pytest.mark.parametrize("secret", [None, [], "", "x" * 4097, "\ud800", "wrong"])
def test_invalid_secret_rejected_without_disclosing_it(secret):
    raw = json.dumps({"capability_sha256": hashlib.sha256(b"synthetic").hexdigest()})
    with pytest.raises(CapabilityError) as error:
        verified_context(raw, secret)
    assert str(error.value) == "Codex job capability mismatch"


@pytest.mark.parametrize("raw", [None, "broken", "[]", "null", "1", '[' * 2000 + ']' * 2000])
def test_invalid_context_rejected(raw):
    with pytest.raises(CapabilityError, match="context is invalid"):
        verified_context(raw, "synthetic")
