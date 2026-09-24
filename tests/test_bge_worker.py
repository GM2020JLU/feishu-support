import io
import json
import sysconfig
from pathlib import Path
from types import SimpleNamespace

import pytest

from k3_support import bge_sandbox, bge_worker


def request():
    return {'operation': 'rerank', 'texts': ['private candidate'], 'query': 'private query',
            'embedding_digest': 'a' * 64, 'reranker_digest': 'b' * 64,
            'embedding_revision': 'c' * 40, 'reranker_revision': 'd' * 40}


def response(value):
    return {'operation': value['operation'], 'result': [.5], 'quality_verified': False,
            'binding': {key: {'revision': value[key + '_revision'],
                            'artifact_digest': value[key + '_digest'], 'verification': 'declared_only'}
                        for key in ('embedding', 'reranker')}}


@pytest.mark.parametrize('score', [True, '0.5', None, float('nan'), float('inf'), -0.1, 1.1, {}])
def test_parent_rejects_invalid_scores(score):
    value = response(request())
    value['result'] = [score]
    with pytest.raises(ValueError):
        bge_sandbox.validate_response(value, request())


@pytest.mark.parametrize('binding', [None, [], {}, {'embedding': None}])
def test_parent_rejects_malformed_binding(binding):
    value = response(request())
    value['binding'] = binding
    with pytest.raises(ValueError):
        bge_sandbox.validate_response(value, request())


@pytest.mark.parametrize('damage', [None, 'dimension', 'zero', 'duplicate', 'negative', 'bool'])
def test_parent_validates_dense_sparse_wire_vectors(damage):
    req = request()
    req['operation'] = 'encode'
    del req['query']
    value = response(req)
    dense = [0.01] * 1024
    sparse = {'indices': [1, 7], 'values': [0.3, 0.8]}
    if damage == 'dimension':
        dense.pop()
    if damage == 'zero':
        dense = [0.] * 1024
    if damage == 'duplicate':
        sparse['indices'] = [1, 1]
    if damage == 'negative':
        sparse['values'][0] = -1
    if damage == 'bool':
        dense[0] = True
    value['result'] = [{'dense': dense, 'sparse': sparse}]
    if damage:
        with pytest.raises(ValueError):
            bge_sandbox.validate_response(value, req)
    else:
        assert bge_sandbox.validate_response(value, req) == value


@pytest.mark.parametrize('change', [
    {'operation': 'shell'}, {'texts': []}, {'texts': ['x'] * 17},
    {'query': 'x' * 4097}, {'embedding_revision': 'main'}, {'command': ['sh']},
])
def test_invalid_protocol_never_loads_a_model(monkeypatch, change):
    monkeypatch.setattr('k3_support.bge_local.load', lambda **kw: pytest.fail('model loaded'))
    with pytest.raises(ValueError):
        bge_worker.execute({**request(), **change})


def test_worker_error_does_not_disclose_input_or_loader_exception(monkeypatch, capsys):
    monkeypatch.setattr('sys.stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(request()).encode())))
    def fail(**kwargs):
        print('private constructor output')
        raise RuntimeError('private exception')
    monkeypatch.setattr('k3_support.bge_local.load', fail)
    assert bge_worker.main() == 1
    output = capsys.readouterr()
    assert output.out == '' and output.err == 'BGE worker failed; no result accepted.\n'


def test_sandbox_mounts_models_readonly_and_bounds_private_stdin(tmp_path, monkeypatch):
    value = request()
    roots = [tmp_path / 'embed', tmp_path / 'rank']
    for root in roots:
        root.mkdir()
    calls = []
    def process(**kwargs):
        calls.append(kwargs)
        assert '--unshare-net' in kwargs['argv']
        assert 'private query' not in str(kwargs['argv'])
        assert kwargs['env'] == {} and json.loads(kwargs['stdin']) == value
        assert kwargs['timeout'] == 19 and kwargs['output_limit'] == bge_worker.MAX_OUTPUT
        for root, target in zip(roots, ['/models/embedding', '/models/reranker']):
            index = kwargs['argv'].index(str(root))
            assert kwargs['argv'][index-1:index+2] == ['--ro-bind', str(root), target]
        return json.dumps(response(value))
    monkeypatch.setattr(bge_sandbox, 'run_process', process)
    result = bge_sandbox.run(value, site_packages=Path(sysconfig.get_paths()['purelib']),
                            embedding_root=roots[0], reranker_root=roots[1], timeout=19)
    assert result['result'] == [.5] and len(calls) == 1


def test_sandbox_rejects_broad_mount_before_process(monkeypatch):
    monkeypatch.setattr(bge_sandbox, 'run_process', lambda **kw: pytest.fail('process'))
    with pytest.raises(ValueError, match='broad'):
        bge_sandbox.run(request(), site_packages=Path(sysconfig.get_paths()['purelib']),
                        embedding_root=Path.home(), reranker_root=Path.home())
