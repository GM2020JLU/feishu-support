"""Real QdrantLocal indexes with deliberately synthetic 2D fixture vectors.

No BGE weights, human Gold, semantic-quality benchmark or network server tested.
"""

from __future__ import annotations

import pytest
from test_knowledge_runtime import ask, entry, choose_first

from k3_support.hybrid_retrieval import BGEProvider, HybridError, QdrantHybrid
from k3_support.knowledge_runtime import corpus_rows

IDENTITY = {
    "provider": "fixture",
    "model_id": "synthetic-2d",
    "revision": "fixture-v1",
    "artifact_digest": "a" * 64,
}


class FixtureEmbedder:
    def __init__(self):
        self.calls = []
        self.on_encode = None

    def encode(self, texts, **options):
        self.calls.append((texts, options))
        if self.on_encode:
            self.on_encode(texts)
        dimensions = [[1.0, 0.0] if "风扇" in text else [0.0, 1.0] for text in texts]
        weights = [{"71": 1.0} if "风扇" in text else {"72": 1.0} for text in texts]
        return {"dense_vecs": dimensions, "lexical_weights": weights}


class FixtureReranker:
    def __init__(self):
        self.calls = []

    def compute_score(self, pairs, *, normalize):
        self.calls.append((pairs, normalize))
        return [0.9 if "风扇" in text else 0.1 for _, text in pairs]


def provider():
    return BGEProvider(
        embedder=FixtureEmbedder(),
        reranker=FixtureReranker(),
        dimensions=2,
        embedding_identity=IDENTITY,
        reranker_identity=IDENTITY,
    )


@pytest.fixture
def local_client():
    qdrant = pytest.importorskip(
        "qdrant_client",
        reason="install pinned hybrid extra for actual local-index tests",
    )
    client = qdrant.QdrantClient(":memory:")
    yield client
    client.close()


def adapter(conn, local_client):
    instance = QdrantHybrid(
        client=local_client, collection="knowledge", provider=provider()
    )
    instance.build(corpus_rows(conn))
    instance.provider.embedder.calls.clear()
    return instance


@pytest.mark.parametrize('query', ['风扇', '今天食堂有什么菜'])
def test_vector_rank_without_semantic_selection_cannot_become_answer(conn, local_client, query):
    entry(conn)
    hybrid = adapter(conn, local_client)
    result = ask(conn, query, options={'backend': 'qdrant'}, hybrid=hybrid)
    assert result['retrieved_knowledge_ids']
    assert result['selected_knowledge_id'] is None
    assert result['selected_entry'] is None
    assert result['abstention_reason'] == 'semantic_selection_required'


def test_semantic_selector_can_abstain_despite_vector_candidates(conn, local_client):
    entry(conn)
    hybrid = adapter(conn, local_client)
    result = ask(conn, '今天食堂有什么菜', options={'backend': 'qdrant'},
                 hybrid=hybrid, selector=lambda query, catalog: None)
    assert result['retrieved_knowledge_ids']
    assert result['selected_knowledge_id'] is None


def test_non_axis_vectors_remain_byte_stable_across_queries(conn, local_client):
    from k3_support.knowledge_runtime import corpus_fingerprint
    entry(conn)
    model = provider()
    model.embedder.encode = lambda texts, **opts: {
        'dense_vecs': [[0.1234567, 0.7654321] for _ in texts],
        'lexical_weights': [{'71': 1.0} for _ in texts]}
    hybrid = QdrantHybrid(client=local_client, collection='stable', provider=model)
    rows = corpus_rows(conn)
    manifest = hybrid.build(rows)
    for _ in range(5):
        result = hybrid.retrieve(query='风扇', eligible=rows,
            corpus_digest=corpus_fingerprint(rows), limit=5)
        assert result['ids']
        assert hybrid._points_digest() == manifest['points_digest']


def test_real_local_dense_sparse_rrf_query_and_acl_first_rerank(conn, local_client):
    wanted = entry(conn, "风扇 public fixture")
    entry(conn, "风扇 private fixture", disclosure="restricted")
    entry(conn, "UFS unrelated fixture")
    hybrid = adapter(conn, local_client)
    observed = ask(conn, "风扇", options={"backend": "qdrant"}, hybrid=hybrid,
                   selector=choose_first)
    assert observed["selected_knowledge_id"] == wanted
    assert observed["trace"]["effective_backend"] == "qdrant"
    assert observed["trace"]["fallback"] is None
    assert len(hybrid.provider.embedder.calls) == 1
    assert hybrid.provider.embedder.calls[0][0] == ["风扇"]
    pairs, normalized = hybrid.provider.reranker.calls[0]
    assert normalized is True
    assert all(
        "private fixture" not in text and "Synthetic answer" not in text
        for _, text in pairs
    )
    assert (
        observed["runtime_binding"]["index"]["provider"]["embedding"]["verification"]
        == "declared_only"
    )
    assert local_client.count("knowledge", exact=True).count == 3


def test_index_binding_stable_on_usage_but_rejects_content_drift(conn, local_client):
    key = entry(conn)
    hybrid = adapter(conn, local_client)
    good = ask(conn, options={"backend": "qdrant"}, hybrid=hybrid)
    conn.execute(
        "UPDATE knowledge_entries SET use_count=17,updated_at='usage' WHERE knowledge_id=?",
        (key,),
    )
    assert (
        ask(conn, options={"backend": "qdrant"}, hybrid=hybrid)["runtime_binding"]
        == good["runtime_binding"]
    )
    conn.execute(
        "UPDATE knowledge_entries SET answer_markdown='风扇 changed' WHERE knowledge_id=?",
        (key,),
    )
    stale = ask(conn, options={"backend": "qdrant"}, hybrid=hybrid)
    assert stale["trace"]["effective_backend"] == "sqlite"
    assert stale["trace"]["fallback"] == "HybridError"
    assert stale["runtime_binding"] != good["runtime_binding"]


def test_paired_query_uses_same_real_input_and_different_backend_binding(
    conn, local_client
):
    wanted = entry(conn)
    hybrid = adapter(conn, local_client)
    inputs = {"query": "风扇", "observed_scope": {"product": "K3"}}
    baseline = ask(conn, **inputs, selector=choose_first)
    vector = ask(conn, **inputs, options={"backend": "qdrant"}, hybrid=hybrid,
                 selector=choose_first)
    assert (
        baseline["selected_knowledge_id"] == vector["selected_knowledge_id"] == wanted
    )
    assert baseline["scope"] == vector["scope"]
    assert baseline["runtime_binding"] != vector["runtime_binding"]
    assert (
        baseline["selected_entry"]["knowledge_query_digest"]
        == vector["selected_entry"]["knowledge_query_digest"]
    )


def test_index_tampering_and_extra_points_invalidate_manifest(conn, local_client):
    from qdrant_client import models

    entry(conn)
    hybrid = adapter(conn, local_client)
    local_client.upsert(
        "knowledge",
        points=[
            models.PointStruct(
                id=99,
                vector={
                    "dense": [1.0, 0.0],
                    "sparse": models.SparseVector(indices=[71], values=[1.0]),
                },
                payload={"knowledge_id": "injected"},
            )
        ],
    )
    result = ask(
        conn, options={"backend": "qdrant", "allow_fallback": False}, hybrid=hybrid
    )
    assert result["selected_entry"] is None
    assert result["trace"]["effective_backend"] == "unavailable"


def test_acl_revocation_during_query_prevents_reranker_and_selector_disclosure(
    conn, local_client
):
    key = entry(conn)
    hybrid = adapter(conn, local_client)
    hybrid.provider.embedder.on_encode = lambda _: conn.execute(
        "UPDATE knowledge_entries SET disclosure_class='restricted' WHERE knowledge_id=?",
        (key,),
    )

    def no_selector(*_):
        pytest.fail("revoked candidate reached selector")

    result = ask(
        conn, options={"backend": "qdrant"}, hybrid=hybrid, selector=no_selector
    )
    assert result["selected_entry"] is None
    assert hybrid.provider.reranker.calls == []


def test_provider_error_safe_fallback_has_distinct_effective_binding(
    conn, local_client
):
    wanted = entry(conn)
    hybrid = adapter(conn, local_client)

    def unavailable(*_):
        raise TimeoutError("fixture timeout")

    hybrid.provider.embedder.on_encode = unavailable
    fallback = ask(conn, options={"backend": "qdrant"}, hybrid=hybrid)
    assert wanted in fallback['retrieved_knowledge_ids']
    assert fallback["selected_knowledge_id"] is None
    assert fallback['abstention_reason'] == 'semantic_selection_required'
    assert fallback["trace"]["fallback"] == "TimeoutError"
    assert fallback["runtime_binding"]["index"] is None


def test_build_never_overwrites_existing_index(conn, local_client):
    entry(conn)
    hybrid = adapter(conn, local_client)
    with pytest.raises(HybridError, match="already exists"):
        hybrid.build(corpus_rows(conn))


@pytest.mark.parametrize(
    "bad",
    [
        {"dense_vecs": [[1]], "lexical_weights": [{"1": 1}]},
        {"dense_vecs": [[float("nan"), 1]], "lexical_weights": [{"1": 1}]},
        {"dense_vecs": [[0, 0]], "lexical_weights": [{"1": 1}]},
        {"dense_vecs": [[1, 0]], "lexical_weights": [{"-1": 1}]},
        {"dense_vecs": [[1, 0]], "lexical_weights": [{"4294967296": 1}]},
        {"dense_vecs": [[1, 0]], "lexical_weights": [{"1": -1}]},
        {"dense_vecs": [[1, 0]], "lexical_weights": [{"1": 1, 1: 2}]},
        {"dense_vecs": [], "lexical_weights": []},
    ],
)
def test_bge_contract_rejects_invalid_vectors(bad):
    instance = provider()
    instance.embedder.encode = lambda *_args, **_kwargs: bad
    with pytest.raises(HybridError):
        instance.encode(["fixture"])


@pytest.mark.parametrize("bad", [[float("inf")], [-0.1], [1.1], [], [True]])
def test_bge_contract_rejects_invalid_reranking(bad):
    instance = provider()
    instance.reranker.compute_score = lambda *_args, **_kwargs: bad
    with pytest.raises(HybridError):
        instance.rerank("query", ["fixture"])


def test_mutable_model_alias_not_an_immutable_binding():
    with pytest.raises(HybridError, match="mutable"):
        BGEProvider(
            embedder=None,
            reranker=None,
            dimensions=2,
            embedding_identity={**IDENTITY, "revision": "latest"},
            reranker_identity=IDENTITY,
        )
