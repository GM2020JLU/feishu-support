"""Test paired smoke mechanics without pretending fixture vectors are BGE."""
import runpy
from pathlib import Path

import pytest

from test_hybrid_retrieval import provider


def test_paired_smoke_uses_both_backends_without_quality_promotion():
    script = Path(__file__).resolve().parents[1] / 'scripts/verify-bge-retrieval.py'
    result = runpy.run_path(str(script))['verify'](provider())
    assert result['quality_verified'] is False
    assert result['release_authorized'] is False
    assert len(result['entries']) == 6
    assert len(result['results']) == 4
    for pair in result['results']:
        for backend in ('sqlite', 'qdrant'):
            assert pair[backend]['trace']['effective_backend'] == backend
            assert set(pair[backend]['retrieved_ids']) <= set(result['entries'])
        assert pair['qdrant']['selected_id'] is None
        assert pair['qdrant']['abstention_reason'] == 'semantic_selection_required'


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / 'knowledge' / 'articles').exists(),
    reason='Internal article corpus is not part of the public source candidate',
)
def test_professional_diagnostic_uses_latest_revisions_without_gold_promotion():
    script = Path(__file__).resolve().parents[1] / 'scripts/verify-bge-retrieval.py'
    root = script.parents[1]/'knowledge'
    namespace = runpy.run_path(str(script))
    entries, queries = namespace['professional_inputs'](root)
    assert len(entries) == len({entry['stable_id'] for entry in entries})
    assert len(queries) == 16
    fan = next(entry for entry in entries if entry['stable_id'] == 'k3.pico.fan.document-route')
    assert fan['source_status'] == 'needs_review'
    progress = []
    result = namespace['verify'](provider(), knowledge_root=root, progress=lambda *counts: progress.append(counts))
    assert progress[-1] == (16,16)
    assert result['corpus_scope'] == 'professional_source_diagnostic_copies'
    assert result['human_reviewed_gold'] is False and result['production_knowledge_changed'] is False
    assert result['release_authorized'] is False
    assert len(result['source_revisions']) == len(entries)
