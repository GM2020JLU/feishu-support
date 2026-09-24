import sys
from types import SimpleNamespace

import pytest

from k3_support import bge_local


def inputs(tmp_path, monkeypatch):
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('TRANSFORMERS_OFFLINE', '1')
    monkeypatch.setattr(bge_local, 'version', lambda _: '1.4.2')
    result = {}
    for name in ('embedding', 'reranker'):
        root = tmp_path / name
        root.mkdir()
        (root / 'config.json').write_text('{"synthetic":true}')
        result.update({name + '_root': root,
                       name + '_digest': bge_local.artifact_identity(root),
                       name + '_revision': 'a' * 40})
    return result


def test_local_factory_uses_checked_paths_and_never_promotes_identity(tmp_path, monkeypatch):
    value = inputs(tmp_path, monkeypatch)
    calls = []
    def construct(path, **options):
        calls.append((path, options))
        return object()
    monkeypatch.setitem(sys.modules, 'FlagEmbedding', SimpleNamespace(
        BGEM3FlagModel=construct, FlagReranker=construct))
    provider = bge_local.load(**value)
    assert [row[0] for row in calls] == [str(value['embedding_root']), str(value['reranker_root'])]
    assert all(row[1] == {'devices': 'cpu', 'use_fp16': False,
                         'trust_remote_code': False, 'batch_size': 8} for row in calls)
    assert provider.binding['embedding']['verification'] == 'declared_only'
    assert provider.binding['embedding']['artifact_digest'] == value['embedding_digest']


@pytest.mark.parametrize('failure', ['offline', 'version', 'changed', 'missing', 'revision', 'symlink'])
def test_invalid_bundle_rejected_before_import_or_constructor(tmp_path, monkeypatch, failure):
    value = inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(bge_local, '_construct', lambda *args: pytest.fail('must not load'))
    if failure == 'offline':
        monkeypatch.delenv('HF_HUB_OFFLINE')
    elif failure == 'version':
        monkeypatch.setattr(bge_local, 'version', lambda _: 'unvalidated')
    elif failure == 'changed':
        (value['embedding_root'] / 'config.json').write_text('{}')
    elif failure == 'missing':
        value['reranker_root'] = tmp_path / 'absent'
    elif failure == 'revision':
        value['embedding_revision'] = 'main'
    else:
        (value['embedding_root'] / 'linked').symlink_to(value['reranker_root'], target_is_directory=True)
    with pytest.raises(ValueError):
        bge_local.load(**value)


def test_constructor_mutation_cannot_return_a_provider(tmp_path, monkeypatch):
    value = inputs(tmp_path, monkeypatch)
    def change(*args):
        (value['reranker_root'] / 'config.json').write_text('{"changed":true}')
        return object(), object()
    monkeypatch.setattr(bge_local, '_construct', change)
    with pytest.raises(ValueError, match='differ'):
        bge_local.load(**value)


@pytest.mark.parametrize('operation,name', [('encode', 'embedding'), ('rerank', 'reranker')])
def test_single_operation_only_constructs_needed_model(tmp_path, monkeypatch, operation, name):
    value = inputs(tmp_path, monkeypatch)
    calls = []
    def construct(path, **options):
        calls.append(path)
        return object()
    monkeypatch.setitem(sys.modules, 'FlagEmbedding', SimpleNamespace(
        BGEM3FlagModel=construct, FlagReranker=construct))
    provider = bge_local.load(**value, operation=operation)
    assert calls == [str(value[name + '_root'])]
    assert provider.binding['embedding']['artifact_digest'] == value['embedding_digest']
    assert provider.binding['reranker']['artifact_digest'] == value['reranker_digest']
    other = 'reranker' if name == 'embedding' else 'embedding'
    (value[other + '_root'] / 'config.json').write_text('{}')
    calls.clear()
    with pytest.raises(ValueError, match='differ'):
        bge_local.load(**value, operation=operation)
    assert calls == []
