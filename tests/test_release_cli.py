"""Exercise the release CLI against private, synthetic, read-only instances."""

import json

import pytest
import yaml
from test_knowledge_release import signed_fixture
from test_runtime_evaluation import bundle_fixture

from k3_support.cli import main


def test_release_check_missing_database_never_initializes(config, capsys):
    config.path.write_text(yaml.safe_dump(config.raw))
    assert not config.database_path.exists()
    assert main(["--config", str(config.path), "knowledge-release-check"]) == 2
    assert not config.database_path.exists()
    assert "existing workflow database" in capsys.readouterr().err


def test_release_cli_actual_replay_is_private_unsigned_and_read_only(conn, config, tmp_path, monkeypatch, capsys):
    cfg, payload, _, _, _ = signed_fixture(conn, config, tmp_path, monkeypatch)
    cfg.path.write_text(yaml.safe_dump(cfg.raw))
    root = bundle_fixture(tmp_path, payload)
    proof = payload["evaluation"]
    contexts = tmp_path / "contexts.json"
    contexts.write_text(json.dumps({item["id"]: {key: value for key, value in item.items() if key not in {"id", "query"}}
                                    for item in proof["request_inputs"]}))
    output = tmp_path / "candidate.json"
    before = list(conn.iterdump())
    argv = ["--config", str(cfg.path), "knowledge-release-prepare", "--gold-bundle", str(root),
            "--approve-gold-digest", proof["gold_manifest_digest"], "--instance-id", "fixture-instance",
            "--key-id", "fixture-key", "--selector", "lexical", "--request-contexts", str(contexts),
            "--evidence-class", "synthetic", "--paired-baseline", "--output", str(output)]
    assert main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["authorized"] is False and result["status"] == "unsigned_owner_review_required"
    assert output.stat().st_mode & 0o777 == 0o600
    assert list(conn.iterdump()) == before
    original = output.read_bytes()
    assert main(argv) == 2
    assert output.read_bytes() == original
    capsys.readouterr()
    assert main(["--config", str(cfg.path), "knowledge-release-check"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["artifact_verified"] and not result["ready"]
    assert result["reason"] == "current_runtime_not_verified"


@pytest.mark.parametrize("value", [[], {"key": "bad"}, {"artifact_path": "relative"},
                                  {"trust_policy_path": "/etc/../tmp/trust"}, {"artifact_path": True}])
def test_release_config_rejects_ambiguous_authority_paths(tmp_path, value):
    from conftest import config_data

    from k3_support.config import ConfigError, validate_config

    raw = config_data(tmp_path)
    raw["knowledge_release"] = value
    with pytest.raises(ConfigError, match="knowledge_release"):
        validate_config(raw)
