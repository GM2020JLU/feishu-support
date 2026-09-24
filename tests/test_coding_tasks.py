import json
from pathlib import Path

import pytest
from test_coding_catalog import configure
from test_executors import executor_config

from k3_support.coding_catalog import choices
from k3_support.coding_tasks import submit
from k3_support.executors import ExecutorError
from k3_support.store import create_case, transition_case


def request_fixture(conn, config, tmp_path, agent="codex"):
    cfg = executor_config(config, codex=True)
    configure(cfg, tmp_path, agent)
    case, _ = create_case(
        conn, title="Operator request", case_type="bug", severity="P2", confidence=0.8
    )
    transition_case(
        conn,
        case_id=case,
        after="triage",
        actor_type="system",
        actor_id=None,
        reason="fixture",
        expected_version=1,
    )
    payload = {
        "case_id": case,
        "case_version": 2,
        "repository": "u-boot",
        "executor_id": "primary",
        "contract_fingerprint": choices(cfg)["items"][0]["contract_fingerprint"],
        "instructions": "Fix the regression",
        "acceptance": "Run the regression test",
        "request_id": "request-1",
    }
    return cfg, payload


@pytest.mark.parametrize("agent", ["codex", "claude", "dsh", "opencode", "hermes"])
def test_create_bound_job_and_replay_receipt(conn, config, tmp_path, agent):
    cfg, payload = request_fixture(conn, config, tmp_path, agent)
    result = submit(conn, cfg, payload)
    assert result["created"] and result["state"] == "queued"
    row = conn.execute(
        "SELECT * FROM jobs WHERE job_id=?", (result["job_id"],)
    ).fetchone()
    context = json.loads(row["context_json"])
    assert context["agent"] == agent and context["model"] == "selected-model"
    assert context["repositories"] == ["u-boot"]
    assert context["operator_request"]["actor"] == cfg.control_operator_id
    snapshot = json.loads(
        conn.execute(
            "SELECT payload_json FROM broker_inputs WHERE job_id=?", (result["job_id"],)
        ).fetchone()[0]
    )
    assert snapshot["context_extra"]["execution"] == context["execution"]
    brief = (Path(row["workdir"]) / "brief.md").read_text()
    assert (
        "`## reply_draft`" in brief and "`requested_actions` (an empty array" in brief
    )
    assert payload["instructions"] in brief and payload["acceptance"] in brief
    assert f'Include the Case ID {payload["case_id"]} in every local commit message' in brief
    assert "Do not wrap the JSON in code fences" in brief
    assert "absolute Case-root" in brief
    assert "exclude pre-existing history" in brief
    assert "exactly these five keys" in brief and "exactly these seven keys" in brief
    assert f'mode=work with repo={payload["repository"]}' in brief
    assert "Include no prose before or after the JSON inside artifacts" in brief
    assert "broker call must\nreturn the test process's own exit code" in brief
    assert "Read or hash the log in\na separate broker call" in brief
    conn.execute("UPDATE jobs SET state='running' WHERE job_id=?", (result["job_id"],))
    # Receipt retrieval does not re-launch or require the former deployment to remain installed.
    Path(
        cfg.raw["coding_executors"]["primary"]["contract_directory"],
        "execution-contract.json",
    ).unlink()
    assert submit(conn, cfg, payload) == {
        "job_id": result["job_id"],
        "state": "running",
        "created": False,
    }
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    with pytest.raises(ValueError, match="different content"):
        submit(conn, cfg, payload | {"instructions": "Different work"})


@pytest.mark.parametrize(
    "mutation",
    [
        {"case_version": 1},
        {"case_version": True},
        {"repository": "/etc"},
        {"contract_fingerprint": "0" * 64},
        {"executor_id": "/bin/sh"},
        {"instructions": " " * 10001 + "x"},
        {"acceptance": ""},
        {"request_id": "../x"},
        {"contract_directory": "/tmp"},
    ],
)
def test_invalid_or_stale_request_creates_no_job(conn, config, tmp_path, mutation):
    cfg, payload = request_fixture(conn, config, tmp_path)
    with pytest.raises((ValueError, ExecutorError)):
        submit(conn, cfg, payload | mutation)
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_case_version_changes_during_artifact_creation(
    conn, config, tmp_path, monkeypatch
):
    cfg, payload = request_fixture(conn, config, tmp_path)
    original = Path.write_text

    def write(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.name == ".capability":
            conn.execute(
                "UPDATE cases SET version=version+1 WHERE case_id=?",
                (payload["case_id"],),
            )
        return result

    monkeypatch.setattr(Path, "write_text", write)
    with pytest.raises(ExecutorError, match="authority changed"):
        submit(conn, cfg, payload)
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert not list(cfg.data_dir.rglob(".capability"))


def test_interleaved_request_cannot_create_two_jobs(
    conn, config, tmp_path, monkeypatch
):
    from k3_support import coding_tasks

    cfg, payload = request_fixture(conn, config, tmp_path)
    original = coding_tasks.create_codex_job
    first = None

    def interleave(*args, **kwargs):
        nonlocal first
        monkeypatch.setattr(coding_tasks, "create_codex_job", original)
        first = submit(conn, cfg, payload)
        return original(*args, **kwargs)

    monkeypatch.setattr(coding_tasks, "create_codex_job", interleave)
    second = submit(conn, cfg, payload)
    assert second["job_id"] == first["job_id"] and not second["created"]
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_interleaved_changed_request_rejects_and_cleans_unused_artifacts(
    conn, config, tmp_path, monkeypatch
):
    from k3_support import coding_tasks

    cfg, payload = request_fixture(conn, config, tmp_path)
    original = coding_tasks.create_codex_job

    def interleave(*args, **kwargs):
        monkeypatch.setattr(coding_tasks, "create_codex_job", original)
        submit(conn, cfg, payload | {"instructions": "Other request won the race"})
        return original(*args, **kwargs)

    monkeypatch.setattr(coding_tasks, "create_codex_job", interleave)
    with pytest.raises(ExecutorError, match="different content"):
        submit(conn, cfg, payload)
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert len(list(cfg.data_dir.rglob(".capability"))) == 1


def test_request_index_upgrade_preserves_legacy_and_rejects_malformed_contexts(
    conn, config, tmp_path
):
    import sqlite3

    from k3_support.db import migration_files

    cfg, payload = request_fixture(conn, config, tmp_path)
    job = submit(conn, cfg, payload)["job_id"]
    conn.execute("DROP INDEX idx_jobs_coding_operator_request")
    row = dict(conn.execute("SELECT * FROM jobs WHERE job_id=?", (job,)).fetchone())
    for index, context in enumerate(["{}", '{"operator_request": null}', "[]"]):
        legacy = row | {
            "job_id": f"legacy-{index}",
            "input_digest": f"legacy-{index}",
            "context_json": context,
        }
        conn.execute(
            f"INSERT INTO jobs({','.join(legacy)}) VALUES({','.join('?' for _ in legacy)})",
            tuple(legacy.values()),
        )
    with pytest.raises(sqlite3.IntegrityError, match="json_valid"):
        conn.execute("UPDATE jobs SET context_json='not-json' WHERE job_id=?", (job,))
    sql = next(sql for version, _, sql in migration_files() if version == 102)
    conn.executescript(sql)
    assert submit(conn, cfg, payload)["created"] is False
    duplicate = row | {"job_id": "duplicate", "input_digest": "different"}
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO jobs({','.join(duplicate)}) VALUES({','.join('?' for _ in duplicate)})",
            tuple(duplicate.values()),
        )
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 4

@pytest.mark.parametrize('state', ['intake', 'paused', 'takeover', 'resolved', 'cancelled', 'error', 'answering'])
def test_operator_submission_cannot_create_undispatchable_job(conn, config, tmp_path, state):
    from k3_support.coding_tasks import options
    cfg, payload = request_fixture(conn, config, tmp_path)
    conn.execute('UPDATE cases SET state=? WHERE case_id=?', (state, payload['case_id']))
    view = options(conn, cfg, {'case_id': payload['case_id']})
    assert view['submission_allowed'] is False and view['case_state'] == state
    with pytest.raises(ValueError, match='Case state does not permit coding'):
        submit(conn, cfg, payload)
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
    assert not list(cfg.data_dir.rglob('.capability'))


def test_replay_retains_existing_receipt_after_case_is_paused(conn, config, tmp_path):
    cfg, payload = request_fixture(conn, config, tmp_path)
    created = submit(conn, cfg, payload)
    conn.execute("UPDATE cases SET state='paused' WHERE case_id=?", (payload['case_id'],))
    replay = submit(conn, cfg, payload)
    assert replay['job_id'] == created['job_id'] and not replay['created']
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
