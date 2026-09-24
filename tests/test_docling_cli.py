import json
import os
from pathlib import Path

from k3_support import docling_cli as cli


def arguments(source, output):
    return [
        "--input",
        str(source),
        "--output",
        str(output),
        "--source-id",
        "synthetic",
        "--source-version",
        "1",
        "--parser-version",
        "fixture",
    ]


def test_export_is_private_and_refuses_overwrite(tmp_path, monkeypatch, capsys):
    source = tmp_path / "a.docx"
    source.write_bytes(b"synthetic")
    output = tmp_path / "evidence.json"
    calls = []

    def convert(content, **kwargs):
        calls.append(content)
        return {
            "status": "unreviewed",
            "private_text": "PRIVATE",
            "conversion": {"source_hash_verified": True},
        }

    monkeypatch.setattr(cli, "convert_document", convert)
    assert cli.main(arguments(source, output)) == 0
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text())["status"] == "unreviewed"
    assert "PRIVATE" not in capsys.readouterr().out
    assert cli.main(arguments(source, output)) == 1
    assert len(calls) == 1
    assert not list(tmp_path.glob(".attachment-pending-*"))


def test_racing_output_is_preserved(tmp_path, monkeypatch):
    source, output = tmp_path / "a.docx", tmp_path / "result.json"
    source.write_bytes(b"synthetic")
    monkeypatch.setattr(
        cli,
        "convert_document",
        lambda *a, **kw: {"conversion": {"source_hash_verified": True}},
    )
    original = os.link

    def raced(staged, target):
        Path(target).write_bytes(b"another result")
        return original(staged, target)

    monkeypatch.setattr(cli.os, "link", raced)
    assert cli.main(arguments(source, output)) == 1
    assert output.read_bytes() == b"another result"
    assert not list(tmp_path.glob(".attachment-pending-*"))


def test_fifo_and_symlink_are_not_read(tmp_path):
    fifo = tmp_path / "a.docx"
    os.mkfifo(fifo)
    assert cli.main(arguments(fifo, tmp_path / "out.json")) == 1
    link = tmp_path / "b.docx"
    link.symlink_to(fifo)
    assert cli.main(arguments(link, tmp_path / "out.json")) == 1
