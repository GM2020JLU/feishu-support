"""Opt-in local Qdrant dense/sparse retrieval and an injected BGE contract.

No implicit network client, model download, or publication. Fixture vectors test
the mechanics, never model quality. Model revision declarations are unverified
until independently attested by the release system.
"""

from __future__ import annotations

import copy
import math
import re
import uuid
from importlib.metadata import version
from typing import Any

from .ids import digest
from .knowledge_runtime import corpus_fingerprint, entry_fingerprint, metadata_text

QDRANT_VERSION = "1.19.0"


class HybridError(RuntimeError):
    pass


def _unit(vector):
    values = [_number(value) for value in vector]
    norm = math.hypot(*values)
    if not math.isfinite(norm) or norm == 0:
        raise HybridError('invalid dense norm')
    return [value / norm for value in values]


def _identity(value: dict[str, Any]) -> dict[str, Any]:
    required = {"provider", "model_id", "revision", "artifact_digest"}
    if (
        not isinstance(value, dict)
        or set(value) != required
        or any(not isinstance(v, str) or not v.strip() for v in value.values())
    ):
        raise HybridError(
            "model identity must include provider, model_id, revision and artifact_digest"
        )
    if value["revision"].lower() in {
        "main",
        "master",
        "latest",
        "default",
    } or not re.fullmatch(r"[a-f0-9]{64}", value["artifact_digest"]):
        raise HybridError("mutable model alias or invalid artifact digest")
    return {**value, "verification": "declared_only"}


def _number(value) -> float:
    if isinstance(value, (str, bool)):
        raise HybridError("vector/score value must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise HybridError("non-finite vector/score")
    return result


class BGEProvider:
    """Wrap preloaded BGEM3FlagModel/FlagReranker-compatible objects.

    The caller is responsible for loading pinned, local artifacts. We never
    interpret a declared revision as proof that those weights were loaded.
    """

    def __init__(
        self,
        *,
        embedder,
        reranker,
        dimensions: int,
        embedding_identity,
        reranker_identity,
    ):
        if type(dimensions) is not int or not 1 <= dimensions <= 65536:
            raise HybridError("invalid dimensions")
        self.embedder = embedder
        self.reranker = reranker
        self.dimensions = dimensions
        self._binding = {
            "contract": "bge-m3-dense-sparse-reranker-v1",
            "dimensions": dimensions,
            "embedding": _identity(embedding_identity),
            "reranker": _identity(reranker_identity),
            "encode_options": {
                "return_dense": True,
                "return_sparse": True,
                "return_colbert_vecs": False,
            },
            "rerank_options": {"normalize": True},
        }

    @property
    def binding(self):
        return copy.deepcopy(self._binding)

    def encode(self, texts: list[str]) -> list[dict[str, Any]]:
        if not texts:
            return []
        raw = self.embedder.encode(texts, **self._binding["encode_options"])
        if (
            not isinstance(raw, dict)
            or "dense_vecs" not in raw
            or "lexical_weights" not in raw
        ):
            raise HybridError("invalid BGE encoding result")
        if len(raw["dense_vecs"]) != len(texts) or len(raw["lexical_weights"]) != len(
            texts
        ):
            raise HybridError("encoding batch length mismatch")
        vectors = []
        for dense, sparse in zip(
            raw["dense_vecs"], raw["lexical_weights"], strict=True
        ):
            dense = [_number(value) for value in dense]
            if len(dense) != self.dimensions or not any(dense):
                raise HybridError("invalid dense vector dimensions/norm")
            if not isinstance(sparse, dict):
                raise HybridError("sparse weights must be a mapping")
            weights = {}
            for token, weight in sparse.items():
                if isinstance(token, bool) or not re.fullmatch(
                    r"0|[1-9][0-9]*", str(token)
                ):
                    raise HybridError("invalid sparse token")
                index = int(token)
                number = _number(weight)
                if index > 2**32 - 1 or number < 0 or index in weights:
                    raise HybridError("invalid sparse weight/index")
                if number:
                    weights[index] = number
            if not weights:
                raise HybridError("empty sparse vector")
            vectors.append(
                {
                    "dense": dense,
                    "sparse": {
                        "indices": sorted(weights),
                        "values": [weights[key] for key in sorted(weights)],
                    },
                }
            )
        return vectors

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        raw = self.reranker.compute_score(
            [[query, text] for text in texts], normalize=True
        )
        if isinstance(raw, (int, float)) and len(texts) == 1:
            raw = [raw]
        if len(raw) != len(texts):
            raise HybridError("rerank result length mismatch")
        scores = [_number(value) for value in raw]
        if any(not 0 <= value <= 1 for value in scores):
            raise HybridError("normalized rerank score out of range")
        return scores


class QdrantHybrid:
    def __init__(
        self, *, client, collection: str, provider: BGEProvider, manifest=None
    ):
        from qdrant_client.local.qdrant_local import QdrantLocal

        if version("qdrant-client") != QDRANT_VERSION:
            raise HybridError("unvalidated qdrant-client version")
        if not isinstance(client._client, QdrantLocal):
            raise HybridError("only explicit local Qdrant clients are supported")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", collection):
            raise HybridError("invalid collection name")
        self.client, self.collection, self.provider = client, collection, provider
        self.location = str(client._client.location)
        self._manifest = copy.deepcopy(manifest)

    @property
    def manifest(self):
        return copy.deepcopy(self._manifest)

    def _points_digest(self):
        points, offset, collected = [], None, []
        while True:
            points, offset = self.client.scroll(
                collection_name=self.collection,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            collected.extend(
                {
                    "id": str(point.id),
                    "payload": point.payload,
                    "vector": point.model_dump(mode="json")["vector"],
                }
                for point in points
            )
            if offset is None:
                break
        return digest(sorted(collected, key=lambda value: value["id"]))

    def build(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Explicit index creation only; never overwrite an existing collection."""
        from qdrant_client import models

        if self.client.collection_exists(self.collection):
            raise HybridError("collection already exists; build a new versioned index")
        if len({row["knowledge_id"] for row in rows}) != len(rows) or any(
            row["status"] != "approved" for row in rows
        ):
            raise HybridError("index rows must be unique approved projections")
        vectors = self.provider.encode([metadata_text(row) for row in rows])
        self.client.create_collection(
            collection_name=self.collection,
            vectors_config={
                "dense": models.VectorParams(
                    size=self.provider.dimensions, distance=models.Distance.DOT
                )
            },
            sparse_vectors_config={"sparse": models.SparseVectorParams()},
        )
        if rows:
            self.client.upsert(
                collection_name=self.collection,
                wait=True,
                points=[
                    models.PointStruct(
                        id=str(uuid.uuid5(uuid.NAMESPACE_URL, row["knowledge_id"])),
                        vector={
                            "dense": _unit(vector["dense"]),
                            "sparse": models.SparseVector(**vector["sparse"]),
                        },
                        payload={
                            "knowledge_id": row["knowledge_id"],
                            "entry_digest": entry_fingerprint(row),
                        },
                    )
                    for row, vector in zip(rows, vectors, strict=True)
                ],
            )
        self._manifest = {
            "version": 2,
            "dense_metric": "unit-dot-v1",
            "qdrant_client": QDRANT_VERSION,
            "collection": self.collection,
            "location": self.location,
            "corpus_digest": corpus_fingerprint(rows),
            "provider": self.provider.binding,
            "points_digest": self._points_digest(),
            "fusion": "rrf-dense-sparse-v1",
        }
        return self.manifest

    def retrieve(
        self,
        *,
        query: str,
        eligible: list[dict[str, Any]],
        corpus_digest: str,
        limit: int,
    ):
        """No unauthorized article text reaches encode/rerank; SQLite filters first."""
        from qdrant_client import models

        manifest = self._manifest
        if (
            not manifest
            or manifest.get('version') != 2
            or manifest.get('dense_metric') != 'unit-dot-v1'
            or manifest.get("corpus_digest") != corpus_digest
            or manifest.get("provider") != self.provider.binding
        ):
            raise HybridError("index/provider manifest is stale or missing")
        if (
            manifest.get("collection") != self.collection
            or manifest.get("location") != self.location
            or manifest.get("points_digest") != self._points_digest()
        ):
            raise HybridError("index contents do not match manifest")
        if not eligible:
            return {"ids": [], "binding": self.manifest}
        allowed = {row["knowledge_id"]: row for row in eligible}
        query_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="knowledge_id", match=models.MatchAny(any=list(allowed))
                )
            ]
        )
        vector = self.provider.encode([query])[0]
        results = self.client.query_points(
            collection_name=self.collection,
            prefetch=[
                models.Prefetch(
                    query=_unit(vector["dense"]),
                    using="dense",
                    filter=query_filter,
                    limit=limit,
                ),
                models.Prefetch(
                    query=models.SparseVector(**vector["sparse"]),
                    using="sparse",
                    filter=query_filter,
                    limit=limit,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        ).points
        found = []
        for point in results:
            key = (point.payload or {}).get("knowledge_id")
            if key not in allowed or point.payload.get(
                "entry_digest"
            ) != entry_fingerprint(allowed[key]):
                raise HybridError("query result escaped its approved snapshot")
            if key not in found:
                found.append(key)
        return {"ids": found, "binding": self.manifest}

    def rerank(self, *, query, rows):
        # Runtime rechecks SQLite after retrieval, before this text disclosure.
        scores = self.provider.rerank(query, [metadata_text(row) for row in rows])
        ranked = sorted(
            zip([row["knowledge_id"] for row in rows], scores, strict=True),
            key=lambda pair: -pair[1],  # Preserve fused/lexical order on ties.
        )
        return [key for key, _ in ranked]
