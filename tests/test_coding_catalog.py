import copy
import json
import os

import pytest
from test_broker_execution_contract import contract

from k3_support.coding_catalog import choices, resolve
from k3_support.config import Config, ConfigError, validate_config


def configure(config, tmp_path, agent="codex"):
    directory = tmp_path / agent
    directory.mkdir(mode=0o700)
    value = {
        **contract(),
        "version": 2,
        "agent": agent,
        "model": "selected-model",
        "reasoning": "high",
        "wire_api": "responses"
        if agent == "codex"
        else "messages"
        if agent == "claude"
        else "chat_completions",
    }
    path = directory / "execution-contract.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    config.raw["coding_executors"] = {
        "primary": {
            "label": "首选工具",
            "contract_directory": str(directory),
            "worker_uid": os.geteuid() + 1,
        }
    }
    validate_config(config.raw)
    return path, value


@pytest.mark.parametrize("agent", ["codex", "claude", "dsh", "opencode", "hermes"])
def test_registered_contract_is_resolved_without_exposing_launch_paths(
    config, tmp_path, agent
):
    path, value = configure(config, tmp_path, agent)
    result = choices(config)
    assert result["worker_health"] == "not_checked"
    entry = result["items"][0]
    assert entry["agent"] == agent and entry["status"] == "configured"
    assert (
        str(tmp_path) not in str(result)
        and "base_url" not in str(result)
        and "worker_uid" not in str(result)
    )
    assert (
        resolve(
            config, "primary", expected_fingerprint=entry["contract_fingerprint"]
        ).model
        == "selected-model"
    )
    path.write_text(json.dumps({**value, "model": "changed-model"}))
    with pytest.raises(ValueError, match="deployment changed"):
        resolve(config, "primary", expected_fingerprint=entry["contract_fingerprint"])
    with pytest.raises(ValueError, match="unknown configured"):
        resolve(config, str(path), expected_fingerprint=entry["contract_fingerprint"])


def test_missing_or_writable_contract_is_unavailable_without_fallback(config, tmp_path):
    path, _ = configure(config, tmp_path)
    path.chmod(0o666)
    assert choices(config)["items"][0]["status"] == "unavailable"
    path.unlink()
    assert choices(config)["items"][0]["status"] == "unavailable"


def test_node_catalogs_do_not_share_process_global_choices(config, tmp_path):
    other = Config(copy.deepcopy(config.raw), config.path)
    configure(config, tmp_path, "codex")
    configure(other, tmp_path, "hermes")
    assert choices(config)["items"][0]["agent"] == "codex"
    assert choices(other)["items"][0]["agent"] == "hermes"


@pytest.mark.parametrize(
    "mutation",
    [
        {"worker_uid": True},
        {"contract_directory": "../private"},
        {"contract_directory": "/path/../private"},
        {"executable": "/bin/sh"},
        {"label": ""},
        {"label": " " + "x" * 80},
        {"contract_directory": "/bad\x00path"},
    ],
)
def test_invalid_catalog_configuration_is_rejected(config, tmp_path, mutation):
    configure(config, tmp_path)
    config.raw["coding_executors"]["primary"].update(mutation)
    with pytest.raises(ConfigError):
        validate_config(config.raw)


def test_console_resolves_named_contract_from_shared_catalog(config, tmp_path):
    path, _ = configure(config, tmp_path, 'hermes')
    path.rename(path.with_name('hermes.json'))
    config.raw['coding_executors']['primary']['contract_name'] = 'hermes.json'
    validate_config(config.raw)
    assert choices(config)['items'][0]['agent'] == 'hermes'
    config.raw['coding_executors']['primary']['contract_name'] = '../hermes.json'
    with pytest.raises(ConfigError):
        validate_config(config.raw)
