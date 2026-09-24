import hashlib
import json
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def module():
    path = Path(__file__).resolve().parents[1] / 'scripts/provision-bge-models.py'
    spec = importlib.util.spec_from_file_location('bge_provision_fixture', path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@pytest.mark.parametrize('lfs', [False, True])
def test_provision_checks_content_not_only_download_success(tmp_path, lfs):
    script = module()
    raw = b'synthetic pinned model bytes'
    path = tmp_path / 'weights'
    path.write_bytes(raw)
    metadata = SimpleNamespace(size=len(raw), blob_id=hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest(),
        lfs=SimpleNamespace(sha256=hashlib.sha256(raw).hexdigest()) if lfs else None)
    assert script.verify_file(path, metadata)['sha256'] == hashlib.sha256(raw).hexdigest()
    path.write_bytes(b'x' * len(raw))
    with pytest.raises(ValueError, match='mismatch'):
        script.verify_file(path, metadata)


def test_metadata_failure_creates_no_destination(tmp_path, monkeypatch):
    script = module()
    def unavailable(*args):
        raise ValueError('synthetic network unavailable')
    monkeypatch.setattr(script, 'metadata_for', unavailable)
    with pytest.raises(ValueError):
        script.provision(tmp_path / 'output', tmp_path / 'cache')
    assert not (tmp_path / 'output').exists()


def test_existing_destination_rejected_before_network(tmp_path, monkeypatch):
    script = module()
    monkeypatch.setattr(script, 'metadata_for', lambda *args: pytest.fail('network'))
    with pytest.raises(ValueError, match='new output'):
        script.provision(tmp_path, tmp_path / 'cache')


def test_metadata_retry_is_bounded_and_keeps_https_verification(monkeypatch):
    script = module()
    def run(argv, **kwargs):
        assert argv[:2] == ['curl', '--disable']
        assert argv[argv.index('--retry')+1] == '2'
        assert argv[argv.index('--proto')+1] == '=https'
        assert '--insecure' not in argv and '-k' not in argv
        assert kwargs['timeout'] == 60
        return SimpleNamespace(stdout=json.dumps({'sha':'a'*40,'siblings':[]}).encode())
    monkeypatch.setattr(script.subprocess, 'run', run)
    assert script.metadata_for('BAAI/bge-m3','a'*40) == {}


@pytest.mark.parametrize('damage', [None, 'digest', 'weights', 'symlink', 'revision'])
def test_offline_bundle_import(tmp_path, monkeypatch, damage):
    script = module()
    monkeypatch.setattr(script, 'metadata_for', lambda *args: pytest.fail('network'))
    source = tmp_path / 'bundle'
    source.mkdir()
    receipt = {'schema': 'k3-bge-provision-v1', 'models': {}, 'quality_verified': False}
    for name, (repo, rev, files) in script.MODELS.items():
        directory = source / name
        directory.mkdir()
        records = {}
        for filename in files:
            raw = filename.encode()
            (directory / filename).write_bytes(raw)
            records[filename] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        receipt['models'][name] = {'repository': repo, 'revision': rev, 'files': records}
    if damage == 'revision':
        receipt['models']['embedding']['revision'] = '0' * 40
    raw = json.dumps(receipt).encode()
    (source / 'provenance.json').write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    weights = source / 'embedding/pytorch_model.bin'
    if damage == 'weights':
        weights.write_bytes(b'changed')
    if damage == 'symlink':
        weights.unlink()
        weights.symlink_to(source / 'embedding/config.json')
    if damage == 'digest':
        digest = '0' * 64
    output = tmp_path / 'imported'
    if damage:
        with pytest.raises(ValueError):
            script.import_bundle(source, output, digest)
        assert not output.exists()
    else:
        result = script.import_bundle(source, output, digest)
        assert result['ok'] and not result['quality_verified']
        assert (output / 'provenance.json').read_bytes() == raw
        assert (output / 'embedding/pytorch_model.bin').read_bytes() == weights.read_bytes()
