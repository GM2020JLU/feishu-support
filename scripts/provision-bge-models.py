#!/usr/bin/env python3
"""Explicit public pinned BGE download; never invoked by inference or startup.

Requires Python and curl. Destination must be new. Failed partial
destinations remain for inspection; no overwrite or automatic cleanup occurs.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

MODELS = {
    'embedding': ('BAAI/bge-m3', '5617a9f61b028005a4858fdac845db406aefb181',
                  ['README.md', 'config.json', 'pytorch_model.bin', 'colbert_linear.pt',
                   'sparse_linear.pt', 'sentencepiece.bpe.model', 'special_tokens_map.json',
                   'tokenizer.json', 'tokenizer_config.json']),
    'reranker': ('BAAI/bge-reranker-v2-m3', '953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e',
                 ['README.md', 'config.json', 'model.safetensors', 'sentencepiece.bpe.model',
                  'special_tokens_map.json', 'tokenizer.json', 'tokenizer_config.json']),
}


def import_bundle(source, output, receipt_sha256):
    """Import an operator-trusted bundle without network or model execution."""
    if output.exists() or output.is_symlink():
        raise ValueError('new output directory required')
    if source.is_symlink() or not source.is_dir():
        raise ValueError('regular bundle directory required')
    receipt_path = source / 'provenance.json'
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ValueError('regular receipt required')
    raw = receipt_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != receipt_sha256:
        raise ValueError('trusted receipt digest mismatch')
    receipt = json.loads(raw)
    if receipt.get('schema') != 'k3-bge-provision-v1' or set(receipt.get('models', {})) != set(MODELS):
        raise ValueError('bundle schema mismatch')
    checked = []
    for name, (repository, revision, files) in MODELS.items():
        record = receipt['models'][name]
        if record.get('repository') != repository or record.get('revision') != revision or set(record.get('files', {})) != set(files):
            raise ValueError('pinned bundle identity mismatch')
        if (source / name).is_symlink():
            raise ValueError('regular model directory required')
        for filename in files:
            path = source / name / filename
            if path.is_symlink() or not path.is_file():
                raise ValueError('regular model file required')
            info = record['files'][filename]
            metadata = SimpleNamespace(size=info['bytes'], lfs=SimpleNamespace(sha256=info['sha256']))
            verify_file(path, metadata)
            checked.append((name, filename, path, metadata))
    output.mkdir(mode=0o700, parents=True)
    for name, filename, path, metadata in checked:
        target = output / name / filename
        target.parent.mkdir(mode=0o700, exist_ok=True)
        shutil.copyfile(path, target)
        verify_file(target, metadata)
        target.chmod(0o400)
    with (output / 'provenance.json').open('xb') as stream:
        stream.write(raw)
    (output / 'provenance.json').chmod(0o400)
    return {'ok': True, 'models': len(MODELS), 'output': str(output), 'quality_verified': False}


def metadata_for(repository, revision):
    response = subprocess.run(['curl', '--disable', '--fail', '--silent', '--show-error', '--proto', '=https',
        '--connect-timeout', '10', '--max-time', '15', '--retry', '2', '--retry-all-errors',
        '--retry-max-time', '40', f'https://huggingface.co/api/models/{repository}/revision/{revision}?blobs=true'],
        check=True, capture_output=True, timeout=60)
    value = json.loads(response.stdout)
    if value['sha'] != revision:
        raise ValueError('model source revision changed')
    return {item['rfilename']: SimpleNamespace(size=item['size'], blob_id=item['blobId'],
            lfs=SimpleNamespace(**item['lfs']) if item.get('lfs') else None)
            for item in value['siblings']}


def download(repository, revision, filename, cache, metadata):
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f'{revision}-{filename}'
    if target.exists():
        verify_file(target, metadata)
        return target
    partial = target.with_name(target.name + '.partial')
    subprocess.run(['curl', '--disable', '--fail', '--location', '--http1.1', '--proto', '=https', '--proto-redir', '=https',
        '--retry', '2', '--retry-all-errors', '--connect-timeout', '20', '--max-time', '900', '--continue-at', '-',
        '--output', str(partial), f'https://huggingface.co/{repository}/resolve/{revision}/{filename}'],
        check=True, timeout=2800)
    verify_file(partial, metadata)
    partial.rename(target)
    return target


def verify_file(path, metadata):
    sha = hashlib.sha256()
    git = hashlib.sha1(f'blob {metadata.size}\0'.encode())
    size = 0
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            sha.update(chunk)
            git.update(chunk)
    if size != metadata.size:
        raise ValueError('model file size mismatch')
    if metadata.lfs is not None:
        if sha.hexdigest() != metadata.lfs.sha256:
            raise ValueError('model LFS hash mismatch')
    elif git.hexdigest() != metadata.blob_id:
        raise ValueError('model Git blob mismatch')
    return {'bytes': size, 'sha256': sha.hexdigest()}


def provision(output, cache):
    if output.exists() or output.is_symlink():
        raise ValueError('new output directory required')
    receipt = {'schema': 'k3-bge-provision-v1', 'models': {}, 'quality_verified': False}
    inventories = {}
    for name, (repository, revision, files) in MODELS.items():
        metadata = metadata_for(repository, revision)
        if any(filename not in metadata for filename in files):
            raise ValueError('pinned model file missing')
        inventories[name] = metadata
    # Network/metadata failures leave no new empty destination behind.
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, (repository, revision, files) in MODELS.items():
        metadata = inventories[name]
        target = output / name
        target.mkdir(mode=0o700)
        records = {}
        for filename in files:
            print(f'{name}: {filename}', flush=True)
            source = download(repository, revision, filename, cache, metadata[filename])
            records[filename] = verify_file(source, metadata[filename])
            shutil.copyfile(source, target / filename)
            if verify_file(target / filename, metadata[filename]) != records[filename]:
                raise ValueError('model copy differs')
            (target / filename).chmod(0o400)
        receipt['models'][name] = {'repository': repository, 'revision': revision, 'files': records}
    path = output / 'provenance.json'
    with path.open('x') as stream:
        json.dump(receipt, stream, indent=2, sort_keys=True)
    path.chmod(0o400)
    return {'ok': True, 'models': len(receipt['models']), 'output': str(output)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--import-bundle', type=Path)
    parser.add_argument('--receipt-sha256')
    args = parser.parse_args()
    if args.import_bundle:
        if args.cache or not args.receipt_sha256:
            parser.error('import requires --receipt-sha256 and excludes --cache')
        result = import_bundle(args.import_bundle, args.output, args.receipt_sha256)
    else:
        if not args.cache or args.receipt_sha256:
            parser.error('download requires --cache and excludes --receipt-sha256')
        result = provision(args.output, args.cache)
    print(json.dumps(result))
