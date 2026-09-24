import copy
import json
from pathlib import Path

import pytest
import yaml
from conftest import config_data
from test_coding_catalog import configure
from test_executors import executor_config

from k3_support.coding_catalog import choices
from k3_support.coding_tasks import submit
from k3_support.config import Config, ConfigError, validate_config
from k3_support.config_migration import preview_config_migration
from k3_support.db import connect, migrate
from k3_support.orchestrator import _codex_brief, _route_repository
from k3_support.store import create_case, transition_case


@pytest.mark.parametrize(
    "value",
    [
        "api",
        None,
        [1],
        [""],
        [" api"],
        ["api\n"],
        ["api", "API"],
        ["x" * 81],
        ["x"] * 33,
    ],
)
def test_invalid_keywords_fail_configuration(config, value):
    cfg = executor_config(config)
    cfg.raw["repositories"]["u-boot"]["routing_keywords"] = value
    with pytest.raises(ConfigError, match="routing_keywords"):
        validate_config(cfg.raw)


def test_explicit_rules_override_legacy_and_ambiguity_does_not_choose(config):
    cfg = executor_config(config)
    cfg.raw["repositories"]["u-boot"]["routing_keywords"] = ["支付接口"]
    cfg.raw["repositories"]["billing"] = dict(
        cfg.raw["repositories"]["u-boot"], routing_keywords=["账单", "支付接口"]
    )
    validate_config(cfg.raw)
    assert _route_repository("bootloader regression", cfg) is None
    assert _route_repository("支付接口故障", cfg) is None
    assert _route_repository("账单无法生成", cfg) == "billing"
    cfg.raw["repositories"]["billing"]["routing_keywords"] = []
    assert _route_repository("账单无法生成", cfg) is None
    assert _route_repository("支付接口故障", cfg) == "u-boot"


def test_legacy_migration_persists_keywords_and_new_schema_avoids_k3_defaults(
    config, tmp_path
):
    cfg = executor_config(config)
    raw = copy.deepcopy(cfg.raw)
    del raw["repositories"]["u-boot"]["routing_keywords"]
    path = tmp_path / "legacy.yaml"
    path.write_text(yaml.safe_dump(raw))
    migrated = preview_config_migration(path)["proposed_config"]
    assert migrated["repositories"]["u-boot"]["routing_keywords"] == [
        "u-boot",
        "uboot",
        "bootloader",
    ]
    assert _route_repository("bootloader issue", Config(migrated, path)) == "u-boot"
    raw["schema_version"] = 2
    modern = Config(validate_config(raw), path)
    assert _route_repository("bootloader issue", modern) is None
    assert _route_repository("U-BOOT issue", modern) == "u-boot"


def test_two_projects_route_and_queue_into_separate_stores(tmp_path):
    observations = []
    for name, agent, keyword in [
        ("commerce", "claude", "checkout"),
        ("analytics", "hermes", "dataset"),
    ]:
        root = tmp_path / name
        root.mkdir()
        raw = config_data(root, mode="active")
        raw["schema_version"] = 2
        raw["features"]["codex"] = True
        remote = Path("/srv/projects") / name
        raw["runtime"] = {
            "remote_host": f"{name}-builder",
            "remote_workspace_root": str(remote),
            "remote_source_root": str(remote / "source"),
            "remote_worktree_root": str(remote / "worktrees"),
        }
        raw["repositories"] = {
            name: {
                "path": str(remote / "source" / name),
                "remote": "origin",
                "base_branch": "main",
                "routing_keywords": [keyword],
            }
        }
        cfg = Config(validate_config(raw), root / "config.yaml")
        configure(cfg, root, agent)
        conn = connect(cfg.database_path)
        try:
            migrate(conn)
            case, _ = create_case(
                conn,
                title="project work",
                case_type="bug",
                severity="P2",
                confidence=0.8,
            )
            transition_case(
                conn,
                case_id=case,
                after="triage",
                actor_type="system",
                actor_id=None,
                reason="test",
                expected_version=1,
            )
            assert _route_repository(keyword.upper() + " failed", cfg) == name
            assert (
                _route_repository(
                    ("dataset" if name == "commerce" else "checkout") + " failed", cfg
                )
                is None
            )
            brief = _codex_brief(
                config=cfg,
                case_id=case,
                repo=name,
                repo_path=cfg.raw["repositories"][name]["path"],
                query=keyword + " failed",
            )
            assert f"{name}-builder" in brief and str(remote / "source" / name) in brief
            assert "K3 issue" not in brief
            payload = {
                "case_id": case,
                "case_version": 2,
                "repository": name,
                "executor_id": "primary",
                "contract_fingerprint": choices(cfg)["items"][0][
                    "contract_fingerprint"
                ],
                "instructions": keyword + " failed",
                "acceptance": "Build and test",
                "request_id": "same-request-id",
            }
            result = submit(conn, cfg, payload)
            job = conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (result["job_id"],)
            ).fetchone()
            context = json.loads(job["context_json"])
            assert context["repositories"] == [name] and context["agent"] == agent
            assert Path(job["workdir"]).is_relative_to(root)
            other = "analytics" if name == "commerce" else "commerce"
            with pytest.raises(ValueError, match="repository is not configured"):
                submit(
                    conn, cfg, payload | {"repository": other, "request_id": "other"}
                )
            assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
            observations.append(
                (cfg.database_path, result["job_id"], context["case_root"])
            )
        finally:
            conn.close()
    assert all(observations[0][index] != observations[1][index] for index in range(3))


def test_software_example_has_explicit_non_hardware_routes():
    from k3_support.config import load_config

    cfg = load_config(
        Path(__file__).parents[1] / "config/software-project.example.yaml"
    )
    assert cfg.raw["schema_version"] == 2 and cfg.raw["mode"] == "shadow"
    assert not cfg.feature("board") and not cfg.feature("codex")
    assert cfg.runtime("remote_host") == "software-builder"
    assert _route_repository("checkout broken", cfg) == "storefront"
    assert _route_repository("报表无法加载", cfg) == "reporting"
    assert _route_repository("checkout analytics", cfg) is None
    assert _route_repository("bootloader kernel regression", cfg) is None
    brief = _codex_brief(
        config=cfg,
        case_id="K3-20260916-0001",
        repo="storefront",
        repo_path=cfg.raw["repositories"]["storefront"]["path"],
        query="checkout broken",
    )
    assert "Hardware operations are disabled" in brief
    assert "USB, serial" not in brief
