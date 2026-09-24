import json
import os
from dataclasses import FrozenInstanceError

import pytest

from k3_support.broker_execution_contract import load_at


def read_contract(tmp_path, value, *, mutation=None):
    path = tmp_path / "execution-contract.json"
    path.write_text(json.dumps(value))
    path.chmod(0o644)
    if mutation:
        mutation(path)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return load_at(fd, control_uid=os.geteuid(), worker_uid=os.geteuid() + 1)
    finally:
        os.close(fd)


def contract():
    return {"version": 1, "provider": "fixture", "base_url": "https://provider.example/v1",
            "model": "gpt-5.6-sol", "reasoning": "medium", "wire_api": "responses"}


def test_valid_contract_is_immutable_and_fingerprints_actual_routing(tmp_path):
    first = read_contract(tmp_path, contract())
    assert len(first.fingerprint) == 64 and first.provider == "fixture"
    with pytest.raises(FrozenInstanceError):
        first.provider = "other"
    second = read_contract(tmp_path, {**contract(), "base_url": "https://other.example/v1"})
    assert first.fingerprint != second.fingerprint


@pytest.mark.parametrize("change", [
    {"api_key": "private"}, {"version": True}, {"model": "other"},
    {"provider": "injected.path"}, {"base_url": "http://provider.example"},
    {"base_url": "https://user:private@provider.example"},
    {"base_url": "https://provider.example?key=private"},
    {"base_url": "https://not-configured.invalid"},
])
def test_unsafe_or_unsupported_contract_is_rejected(tmp_path, change):
    with pytest.raises(ValueError):
        read_contract(tmp_path, {**contract(), **change})


@pytest.mark.parametrize("kind", ["writable", "symlink", "hardlink", "fifo"])
def test_contract_reader_rejects_unsafe_file_types(tmp_path, kind):
    def mutate(path):
        if kind == "writable":
            path.chmod(0o666)
        elif kind == "symlink":
            target = path.with_name("original.json")
            path.rename(target)
            path.symlink_to(target)
        elif kind == "hardlink":
            os.link(path, path.with_name("alias.json"))
        else:
            path.unlink()
            os.mkfifo(path, 0o600)
    with pytest.raises((ValueError, OSError)):
        read_contract(tmp_path, contract(), mutation=mutate)
