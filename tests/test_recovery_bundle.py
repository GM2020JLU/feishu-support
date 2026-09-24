import json

import pytest
import yaml

from k3_support.operations import OperationsError, restore_probe
from k3_support.recovery_bundle import create, verify
from k3_support.store import ingest_event


def setup(config, conn, tmp_path):
    config.path.write_text(yaml.safe_dump(config.raw))
    path = config.data_dir / "attachments" / "raw.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"private source")
    ingest_event(conn, source="feishu_bot_im", identity="bot", external_id="fixture",
                 payload={}, occurred_at="2026-09-08T00:00:00+00:00", raw_artifact_path=str(path))
    # Destination is a sibling of the instance, never under its data root.
    return path, tmp_path.parent / (tmp_path.name + "-bundle")


def test_bundle_copies_real_database_files_and_configuration(config, conn, tmp_path):
    path, target = setup(config, conn, tmp_path)
    before = conn.serialize()
    result = create(config, target)
    assert result["backup_created"] and not result["activation_allowed"]
    manifest = json.loads((target / "manifest.json").read_text())
    assert {entry["path"] for entry in manifest["entries"]} == {
        "database.db", "configuration.yaml", "data/attachments/raw.txt"}
    assert (target / "data/attachments/raw.txt").read_bytes() == path.read_bytes()
    assert (target / "configuration.yaml").read_bytes() == config.path.read_bytes()
    assert restore_probe(target / "database.db")["integrity"]["ok"]
    assert conn.serialize() == before
    assert verify(target)["integrity_verified"]
    assert not verify(target)["activation_allowed"]
    assert not (target / "INCOMPLETE").exists()
    assert target.stat().st_mode & 0o077 == 0
    assert all((target / item["path"]).stat().st_mode & 0o077 == 0 for item in manifest["entries"])
    with pytest.raises(FileExistsError):
        create(config, target)


@pytest.mark.parametrize("kind", ["symlink", "missing", "directory_link", "database_link"])
def test_bundle_rejects_incomplete_or_linked_inputs(config, conn, tmp_path, kind):
    path, target = setup(config, conn, tmp_path)
    if kind == "missing":
        path.unlink()
    elif kind == "symlink":
        path.unlink()
        path.symlink_to(config.path)
    elif kind == "directory_link":
        (path.parent / "link").symlink_to(tmp_path, target_is_directory=True)
    else:
        original = config.database_path
        link = tmp_path / "database-link"
        link.symlink_to(original)
        config.raw["paths"]["database"] = str(link)
    with pytest.raises((OperationsError, OSError)):
        create(config, target)
    assert not (target / "manifest.json").exists()


@pytest.mark.parametrize("kind", ["bytes", "path", "missing", "incomplete", "duplicate", "link", "extra"])
def test_bundle_verifier_rejects_tampering(config, conn, tmp_path, kind):
    path, target = setup(config, conn, tmp_path)
    create(config, target)
    payload = target / "data/attachments/raw.txt"
    manifest_path = target / "manifest.json"
    if kind == "bytes":
        payload.write_bytes(b"changed")
    elif kind == "missing":
        payload.unlink()
    elif kind == "incomplete":
        (target / "INCOMPLETE").touch()
    elif kind == "extra":
        (target / "unlisted-secret").write_bytes(b"not in manifest")
    elif kind == "link":
        payload.unlink()
        payload.symlink_to(path)
    else:
        data = json.loads(manifest_path.read_text())
        if kind == "path":
            data["entries"][2]["path"] = "../configuration.yaml"
        else:
            data["entries"].append(data["entries"][0])
        manifest_path.write_text(json.dumps(data))
    with pytest.raises((OperationsError, OSError)):
        verify(target)


def test_bundle_detects_database_change_during_file_copy(config, conn, tmp_path, monkeypatch):
    import k3_support.recovery_bundle as bundle

    _path, target = setup(config, conn, tmp_path)
    original = bundle._copy

    def mutate(*args):
        result = original(*args)
        conn.execute("UPDATE inbound_events SET payload_json='{}',external_id='changed'")
        return result

    monkeypatch.setattr(bundle, "_copy", mutate)
    with pytest.raises(OperationsError, match="changed"):
        create(config, target)
    assert (target / "INCOMPLETE").exists()
    assert not (target / "manifest.json").exists()


def test_bundle_cli_can_verify_a_relocated_copy(config, conn, tmp_path, capsys):
    import shutil

    from k3_support import cli

    _, target = setup(config, conn, tmp_path)
    before = conn.serialize()
    assert cli.main(["--config", str(config.path), "recovery-bundle", "--output", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["backup_created"]
    copied = target.with_name(target.name + "-copied")
    shutil.copytree(target, copied)
    assert cli.main(["recovery-bundle-verify", str(copied)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["integrity_verified"] and not result["authenticity_verified"]
    assert not result["activation_allowed"]
    assert conn.serialize() == before


def test_bundle_rejects_old_schema_without_migrating(config, conn, tmp_path):
    _, target = setup(config, conn, tmp_path)
    conn.execute("DELETE FROM schema_migrations WHERE version=(SELECT max(version) FROM schema_migrations)")
    before = conn.serialize()
    with pytest.raises(OperationsError, match="schema"):
        create(config, target)
    assert conn.serialize() == before
    assert not (target / "manifest.json").exists()
