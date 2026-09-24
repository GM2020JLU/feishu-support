"""Explicit CPU loader for separately provisioned BGE artifacts.

Run in a supervised network-disabled process with read-only model mounts.
Environment flags are an additional library hint, not an OS network boundary.
No downloads, installation, index creation or release promotion are performed.
"""
import os
import re
from importlib.metadata import version
from pathlib import Path

from .docling_models import inventory
from .hybrid_retrieval import BGEProvider
from .ids import digest

FLAG_VERSION = '1.4.2'


def artifact_identity(root):
    root = Path(root)
    if not root.is_absolute() or '..' in root.parts:
        raise ValueError('absolute local model directory required')
    files = inventory(root)['files']
    return digest({'schema': 'k3-bge-local-artifacts-v1', 'files': files})


def _check(root, expected, revision):
    if not isinstance(revision, str) or not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('pinned model source commit required')
    if not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected):
        raise ValueError('expected model artifact digest required')
    if artifact_identity(root) != expected:
        raise ValueError('local model artifacts differ from expected digest')


def _construct(embedding_root, reranker_root, operation=None):
    # Imported only after explicit offline setup, version and artifact checks.
    from FlagEmbedding import BGEM3FlagModel, FlagReranker

    options = {'devices': 'cpu', 'use_fp16': False, 'trust_remote_code': False,
               'batch_size': 8}
    return (BGEM3FlagModel(str(embedding_root), **options) if operation != 'rerank' else None,
            FlagReranker(str(reranker_root), **options) if operation != 'encode' else None)


def load(*, embedding_root, embedding_digest, embedding_revision,
         reranker_root, reranker_digest, reranker_revision, operation=None):
    """Load an explicitly pinned pair; do not claim human approval or quality.

    Source commit labels still require independent provenance review. Content
    hashing proves the supplied bytes match the expected digests, not that a
    revision label is authentic. The existing provider stays declared_only.
    """
    if operation not in (None, 'encode', 'rerank'):
        raise ValueError('unsupported model loading operation')
    if any(os.environ.get(key) != '1' for key in ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE')):
        raise ValueError('start an isolated worker with explicit offline model settings')
    if version('FlagEmbedding') != FLAG_VERSION:
        raise ValueError('unsupported FlagEmbedding runtime version')
    pairs = [(Path(embedding_root), embedding_digest, embedding_revision),
             (Path(reranker_root), reranker_digest, reranker_revision)]
    for args in pairs:
        _check(*args)
    embedder, reranker = (_construct(pairs[0][0], pairs[1][0]) if operation is None
                         else _construct(pairs[0][0], pairs[1][0], operation))
    for args in pairs:
        _check(*args)  # Reject a changed bundle before returning any provider.
    provider = BGEProvider(embedder=embedder, reranker=reranker, dimensions=1024,
        embedding_identity={'provider': 'local', 'model_id': 'BAAI/bge-m3',
                            'revision': embedding_revision, 'artifact_digest': embedding_digest},
        reranker_identity={'provider': 'local', 'model_id': 'BAAI/bge-reranker-v2-m3',
                           'revision': reranker_revision, 'artifact_digest': reranker_digest})
    return provider
