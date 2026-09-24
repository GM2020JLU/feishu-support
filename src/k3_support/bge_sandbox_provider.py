"""Explicit local sandbox adapter for QdrantHybrid; no downloads or activation."""
import time

from . import bge_sandbox
from .bge_worker import validate
from .hybrid_retrieval import BGEProvider


class SandboxedBGEProvider:
    """Run bounded batches in isolated processes, with one total call budget."""

    dimensions = 1024

    def __init__(self, *, site_packages, embedding_root, reranker_root,
                 embedding_digest, embedding_revision, reranker_digest,
                 reranker_revision, timeout=300):
        if type(timeout) not in (int, float) or not 0 < timeout <= 300:
            raise ValueError('timeout must be 0..300 seconds')
        self.timeout = timeout
        self.mounts = dict(site_packages=site_packages, embedding_root=embedding_root,
                           reranker_root=reranker_root)
        self.identities = dict(embedding_digest=embedding_digest, embedding_revision=embedding_revision,
                               reranker_digest=reranker_digest, reranker_revision=reranker_revision)
        validate(dict(operation='encode', texts=['identity validation'], **self.identities))
        self._contract = BGEProvider(embedder=None, reranker=None, dimensions=1024,
            embedding_identity=dict(provider='local', model_id='BAAI/bge-m3',
                                    revision=embedding_revision, artifact_digest=embedding_digest),
            reranker_identity=dict(provider='local', model_id='BAAI/bge-reranker-v2-m3',
                                   revision=reranker_revision, artifact_digest=reranker_digest))

    @property
    def binding(self):
        return self._contract.binding

    def _call(self, operation, texts, query=None):
        if not isinstance(texts, list):
            raise ValueError('texts must be a list')
        batches = []
        for start in range(0, len(texts), 16):
            request = dict(operation=operation, texts=texts[start:start+16], **self.identities)
            if operation == 'rerank':
                request['query'] = query
            validate(request)  # Validate every batch before starting any process.
            batches.append(request)
        deadline = time.monotonic() + self.timeout
        results = []
        for request in batches:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('BGE total call budget exhausted')
            value = bge_sandbox.run(request, **self.mounts, timeout=remaining)
            if value['binding'] != self.binding:
                raise ValueError('BGE runtime contract differs')
            results.extend(value['result'])
        return results

    def encode(self, texts):
        return self._call('encode', texts)

    def rerank(self, query, texts):
        return self._call('rerank', texts, query)
