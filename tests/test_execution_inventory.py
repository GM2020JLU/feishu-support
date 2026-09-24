import json

import pytest
from test_coding_budget import setup

from k3_support.execution_inventory import page


def test_job_metadata_and_physical_unknown_are_readonly(conn, config):
    cfg, job = setup(conn, config)
    conn.execute(
        "UPDATE jobs SET workdir='/private/secret',context_json='{\"secret\":\"hidden\"}' WHERE job_id=?",
        (job,),
    )
    before = list(conn.iterdump())
    result = page(conn, cfg)
    assert result["items"][0]["job_id"] == job
    assert result["read_only"] and result["board"]["physical_state"] == "unknown"
    assert result["board"]["lease"] is None
    assert "secret" not in str(result) and "workdir" not in str(result)
    assert list(conn.iterdump()) == before


def test_execution_filter_and_keyset(conn, config):
    ids = []
    for _ in range(4):
        cfg, job = setup(conn, config)
        ids.append(job)
    ids.sort()
    conn.execute("UPDATE jobs SET state='failed' WHERE job_id=?", (ids[0],))
    first = page(conn, cfg, limit=1)
    assert first["total_matching"] == 3
    assert first["items"][0]["job_id"] == ids[1]
    second = page(conn, cfg, after_id=first["next_cursor"])
    assert [r["job_id"] for r in second["items"]] == ids[2:]
    assert page(conn, cfg, state="failed")["items"][0]["job_id"] == ids[0]


def test_expired_lock_is_not_released_or_claimed_idle(conn, config):
    conn.execute(
        "INSERT INTO locks VALUES('board1','secret-owner',NULL,'board','2000-01-01T00:00:00+00:00','2000-01-01T00:00:00+00:00','2000-01-01T00:00:00+00:00','{}')"
    )
    before = list(conn.iterdump())
    result = page(conn, config)
    assert result["board"]["lease"]["expiry_state"] == "expired"
    assert result["board"]["physical_state"] == "unknown"
    assert "secret-owner" not in str(result)
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize(
    "kwargs", [{"state": []}, {"state": "invalid"}, {"after_id": None}, {"limit": True}]
)
def test_bad_filters(conn, config, kwargs):
    with pytest.raises(ValueError):
        page(conn, config, **kwargs)


@pytest.mark.parametrize("agent", ["codex", "claude", "dsh", "opencode", "hermes"])
def test_task_bound_identity_is_public_without_private_context(conn, config, agent):
    cfg, job = setup(conn, config)
    context = {
        "agent": agent,
        "model": "bound-model",
        "reasoning": "high",
        "execution": {"agent": agent, "contract_fingerprint": "a" * 64},
        "private_secret": "must-not-leak",
        "case_root": "/private/workspace",
    }
    conn.execute(
        "UPDATE jobs SET context_json=? WHERE job_id=?", (json.dumps(context), job)
    )
    result = page(conn, cfg)
    identity = result["items"][0]["coding_identity"]
    assert identity["agent"] == agent and identity["model"] == "bound-model"
    assert (
        identity["reasoning"] == "high" and identity["binding"] == "deployment_contract"
    )
    assert "must-not-leak" not in str(result) and "/private" not in str(result)


@pytest.mark.parametrize(
    "context",
    [
        None,
        [],
        {"agent": "claude"},
        {
            "agent": "claude",
            "execution": {"agent": "codex", "contract_fingerprint": "a" * 64},
        },
        {"agent": "codex", "model": "<script>bad</script>", "reasoning": "high"},
    ],
)
def test_invalid_task_identity_is_not_guessed(conn, config, context):
    cfg, job = setup(conn, config)
    conn.execute(
        "UPDATE jobs SET context_json=? WHERE job_id=?", (json.dumps(context), job)
    )
    assert page(conn, cfg)["items"][0]["coding_identity"] == {"binding": "unknown"}
