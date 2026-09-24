#!/usr/bin/env python3
"""Explicit real-weight offline smoke test with public synthetic text only."""
import argparse
import json
import time

from k3_support.bge_sandbox_provider import SandboxedBGEProvider
from k3_support.ids import digest


def verify(provider):
    texts = ['风扇转速调整方法与自动温控恢复。', '文件系统分区与启动介质的只读检查。']
    started = time.monotonic()
    vectors = provider.encode(texts)
    encoded = time.monotonic()
    scores = provider.rerank('如何调整风扇转速？', texts)
    finished = time.monotonic()
    if len(vectors) != 2 or len(scores) != 2:
        raise ValueError('incomplete real-model smoke response')
    return {'schema': 'k3-bge-inference-smoke-v1', 'binding': provider.binding,
            'vector_digest': digest(vectors), 'dimensions': provider.dimensions,
            'sparse_counts': [len(item['sparse']['indices']) for item in vectors],
            'rerank_scores': scores, 'encode_seconds': round(encoded-started, 3),
            'rerank_seconds': round(finished-encoded, 3), 'synthetic_inputs': True,
            'quality_verified': False, 'release_authorized': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('site-packages', 'embedding-root', 'reranker-root',
                 'embedding-digest', 'embedding-revision', 'reranker-digest', 'reranker-revision'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args()
    print(json.dumps(verify(SandboxedBGEProvider(**vars(args))), ensure_ascii=False))
