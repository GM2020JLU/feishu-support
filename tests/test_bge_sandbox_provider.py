import pytest

from k3_support import bge_sandbox_provider as module


def provider():
    return module.SandboxedBGEProvider(site_packages='/deps', embedding_root='/embedding',
        reranker_root='/reranker', embedding_digest='a'*64, reranker_digest='b'*64,
        embedding_revision='c'*40, reranker_revision='d'*40, timeout=30)


def test_batches_preserve_order_and_contract(monkeypatch):
    p = provider()
    calls = []
    def run(request, **kwargs):
        calls.append(request)
        assert 0 < kwargs['timeout'] <= 30
        return {'binding': p.binding, 'result': request['texts']}
    monkeypatch.setattr(module.bge_sandbox, 'run', run)
    texts = [str(n) for n in range(35)]
    assert p.encode(texts) == texts
    assert [len(c['texts']) for c in calls] == [16, 16, 3]
    assert p.binding['embedding']['verification'] == 'declared_only'


def test_invalid_later_batch_never_starts_process(monkeypatch):
    monkeypatch.setattr(module.bge_sandbox, 'run', lambda *a, **k: pytest.fail('process'))
    with pytest.raises(ValueError):
        provider().encode(['valid'] * 16 + [''])


def test_total_budget_not_reset_per_batch(monkeypatch):
    p = provider()
    times = iter([0, 1, 31])
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(times))
    calls = []
    def run(request, **kwargs):
        calls.append(request)
        return {'binding': p.binding, 'result': [0.5] * len(request['texts'])}
    monkeypatch.setattr(module.bge_sandbox, 'run', run)
    with pytest.raises(TimeoutError):
        p.rerank('query', ['candidate'] * 17)
    assert len(calls) == 1


def test_contract_change_rejects_all_results(monkeypatch):
    monkeypatch.setattr(module.bge_sandbox, 'run', lambda *a, **k: {'binding': {}, 'result': [.5]})
    with pytest.raises(ValueError, match='contract'):
        provider().rerank('query', ['candidate'])
