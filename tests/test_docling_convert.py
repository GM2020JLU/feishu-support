import hashlib
import json
from pathlib import Path

import pytest

from k3_support import docling_convert as module


@pytest.mark.parametrize("format", ["pdf", "png", "jpg", "tiff"])
def test_model_formats_require_explicit_model_identity(format, tmp_path):
    with pytest.raises(ValueError, match="requires a provisioned"):
        module.convert_document(
            b"fixture",
            format=format,
            source_id="id",
            source_version="1",
            site_packages=tmp_path,
            expected_version="fixture",
        )


def test_office_does_not_accept_model_mount(tmp_path):
    with pytest.raises(ValueError, match="does not use model"):
        module.convert_document(
            b"fixture",
            format="docx",
            source_id="id",
            source_version="1",
            site_packages=tmp_path,
            expected_version="fixture",
            models=tmp_path,
            model_digest="a" * 64,
        )


def test_snapshot_is_bound_and_cleaned(monkeypatch, tmp_path):
    snapshot_paths = []
    monkeypatch.setattr(
        module,
        "sandbox_command",
        lambda **kw: ["bwrap", "--unshare-net", "--", "python"],
    )

    def process(**kwargs):
        argv = kwargs["argv"]
        snapshot = Path(argv[argv.index("--ro-bind") + 1])
        snapshot_paths.append(snapshot)
        assert snapshot.read_bytes() == b"office-fixture"
        assert snapshot.stat().st_mode & 0o777 == 0o400
        assert kwargs["env"] == {}
        assert "--unshare-net" in argv
        return json.dumps(
            {
                "parser_version": "fixture",
                "source_sha256": hashlib.sha256(b"office-fixture").hexdigest(),
                "document": {
                    "schema_name": "DoclingDocument",
                    "version": "1.7.0",
                    "body": {"self_ref": "#/body"},
                    "furniture": {"self_ref": "#/furniture"},
                },
            }
        )

    monkeypatch.setattr(module, "run_process", process)
    result = module.convert_office(
        b"office-fixture",
        format="docx",
        source_id="id",
        source_version="1",
        site_packages=tmp_path,
        expected_version="fixture",
    )
    assert result["conversion"]["source_hash_verified"] is True
    assert result["automatic_reply_eligible"] is False
    assert not snapshot_paths[0].exists()


@pytest.mark.parametrize("format", ["pdf", "../docx", "https://example.com", "docm"])
def test_unsupported_format_fails_before_process(format, tmp_path):
    with pytest.raises(ValueError, match="supported Office"):
        module.convert_office(
            b"x",
            format=format,
            source_id="id",
            source_version="1",
            site_packages=tmp_path,
            expected_version="fixture",
        )


def test_failed_process_is_not_retried_and_snapshot_removed(monkeypatch, tmp_path):
    snapshots = []
    monkeypatch.setattr(
        module, "sandbox_command", lambda **kw: ["bwrap", "--", "python"]
    )

    def fail(**kw):
        snapshots.append(Path(kw["argv"][kw["argv"].index("--ro-bind") + 1]))
        raise TimeoutError("fixture timeout")

    monkeypatch.setattr(module, "run_process", fail)
    with pytest.raises(TimeoutError):
        module.convert_office(
            b"x",
            format="docx",
            source_id="id",
            source_version="1",
            site_packages=tmp_path,
            expected_version="fixture",
        )
    assert len(snapshots) == 1
    assert not snapshots[0].exists()
