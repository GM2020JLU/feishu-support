import importlib.util
from pathlib import Path


def test_smoke_uses_only_synthetic_inputs_and_never_promotes_quality():
    path = Path(__file__).resolve().parents[1] / 'scripts/verify-bge-inference.py'
    spec = importlib.util.spec_from_file_location('bge_smoke', path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    class FakeProvider:
        dimensions = 1024
        binding = {'fixture_only': True}
        def encode(self, texts):
            assert len(texts) == 2 and '风扇' in texts[0]
            return [{'dense':[.1]*1024,'sparse':{'indices':[1],'values':[.5]}} for _ in texts]
        def rerank(self, query, texts):
            assert query == '如何调整风扇转速？' and len(texts) == 2
            return [.8,.2]
    result = script.verify(FakeProvider())
    assert result['synthetic_inputs'] and not result['quality_verified']
    assert not result['release_authorized']
    assert result['sparse_counts'] == [1,1] and len(result['vector_digest']) == 64
