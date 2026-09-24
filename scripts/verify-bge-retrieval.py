#!/usr/bin/env python3
"""Real-model paired retrieval smoke; synthetic data, no release authority."""
import argparse
import json
import time
import traceback
from pathlib import Path

from qdrant_client import QdrantClient
from k3_support.bge_sandbox_provider import SandboxedBGEProvider
from k3_support.db import connect, migrate
from k3_support.hybrid_retrieval import QdrantHybrid
from k3_support.ids import digest
from k3_support.knowledge import create_candidate, review
from k3_support.knowledge_runtime import corpus_rows, query_knowledge


class DiagnosticHybrid(QdrantHybrid):
    """Expose failures hidden by runtime fallback, on synthetic inputs only."""
    def retrieve(self, **kwargs):
        try:
            return super().retrieve(**kwargs)
        except Exception:
            traceback.print_exc()
            raise

    def rerank(self, **kwargs):
        try:
            return super().rerank(**kwargs)
        except Exception:
            traceback.print_exc()
            raise


def professional_inputs(root):
    """Newest source revisions as diagnostic copies, never approve the originals."""
    from k3_support.professional_knowledge import load_article
    latest = {}
    for path in sorted((Path(root)/'articles').rglob('*.md')):
        article = load_article(path)
        key = article.metadata['id']
        if key not in latest or article.metadata['revision'] > latest[key].metadata['revision']:
            latest[key] = article
    if not latest:
        raise ValueError('no professional articles found')
    entries = []
    for key, article in sorted(latest.items()):
        metadata = article.metadata
        entries.append({'title': metadata['title'],
            'questions': metadata['intent']['aliases'] + metadata['intent']['question_examples'],
            'answer': article.body_markdown, 'stable_id': key,
            'revision_digest': article.revision_digest,
            'source_status': metadata['status']})
    # Authored diagnostic probes, not human-reviewed Gold or tuning targets.
    queries = ['pico太吵了怎么让风扇慢点', '板子上的小控制器固件去哪里升级',
               '系统还没起来，能在启动命令行更新EC吗', '开机一下就进系统了怎么停住',
               '每次重启我刚改的变量就没了', '想知道这版固件到底编进了哪些命令',
               '不改盘上的数据，怎么看有哪些分区', '怎么把分区里的文件读到内存',
               '插了固态硬盘，怎么优先从它启动', 'UFS没找到和文件读不出来是一回事吗',
               '串口只打到SPL，后面应该轮到谁', '恢复默认环境会不会把已保存的配置覆盖',
               '今天食堂有什么菜', '帮我批准这个采购订单',
               '把所有硬盘格式化就能解决启动失败吧', '你保证客户今晚一定能验收通过吗']
    return entries, queries


def verify(provider, *, knowledge_root=None, progress=None):
    conn = connect(':memory:')
    client = QdrantClient(':memory:')
    try:
        migrate(conn)
        topics = ['Pico 风扇转速调整与自动温控恢复', 'EC 固件升级与版本查询',
                  'U-Boot 命令行进入方法', 'SSD 启动介质切换',
                  'UFS 分区与文件系统只读检查', 'U-Boot 环境变量查看与保存']
        entries = [dict(title=title, questions=[title], answer='Synthetic retrieval fixture only') for title in topics]
        queries = ['pico太吵了怎么让风扇慢点', '如何升级嵌入式控制器固件',
                   '开机时怎么打断自动启动进入命令行', '今天食堂有什么菜']
        if knowledge_root is not None:
            entries, queries = professional_inputs(knowledge_root)
        provenance = {}
        for entry in entries:
            title = entry['title']
            key = create_candidate(conn, title=title, questions=entry['questions'],
                answer_markdown=entry['answer'], project='K3',
                module='bootloader', software_version=None, disclosure_class='public',
                confidence=.99, source_authority=.99, canonical_case_id=None,
                source_digest=digest(title))
            review(conn, knowledge_id=key, reviewer_id='synthetic-benchmark', decision='approved')
            provenance[key] = {k: v for k, v in entry.items() if k not in {'answer','questions'}}
        hybrid = DiagnosticHybrid(client=client, collection='synthetic', provider=provider)
        start = time.monotonic()
        hybrid.build(corpus_rows(conn))
        build_seconds = time.monotonic() - start
        results = []
        for query in queries:
            pair = {'query': query}
            for backend in ('sqlite', 'qdrant'):
                start = time.monotonic()
                result = query_knowledge(conn, query=query, requester_id='synthetic-peer',
                    chat_id='synthetic-chat', options={'backend': backend,
                        'qdrant_collection': hybrid.collection, 'allow_fallback': False},
                    hybrid=hybrid if backend == 'qdrant' else None)
                pair[backend] = {'seconds': round(time.monotonic()-start, 3),
                    'retrieved_ids': result['retrieved_knowledge_ids'],
                    'selected_id': result['selected_knowledge_id'],
                    'abstention_reason': result['abstention_reason'], 'trace': result['trace']}
                if result['trace']['effective_backend'] != backend:
                    raise RuntimeError('requested retrieval backend fell back')
            results.append(pair)
            if progress:
                progress(len(results), len(queries))
        return {'schema': 'k3-bge-paired-smoke-v1', 'synthetic_inputs': True,
                'quality_verified': False, 'release_authorized': False,
                'human_reviewed_gold': False, 'production_knowledge_changed': False,
                'corpus_scope': 'professional_source_diagnostic_copies' if knowledge_root else 'synthetic',
                'source_revisions': provenance,
                'binding': provider.binding, 'build_seconds': round(build_seconds, 3),
                'entries': {r['knowledge_id']: r['title'] for r in corpus_rows(conn)},
                'results': results}
    finally:
        client.close()
        conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('site-packages', 'embedding-root', 'reranker-root',
                 'embedding-digest', 'embedding-revision', 'reranker-digest', 'reranker-revision'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--knowledge-root', type=Path)
    parser.add_argument('--output', type=Path)
    args = vars(parser.parse_args())
    root, output = args.pop('knowledge_root'), args.pop('output')
    if output is not None and output.exists():
        raise ValueError('refuse to overwrite evaluation output')
    result = verify(SandboxedBGEProvider(**args), knowledge_root=root,
                    progress=lambda done,total: print(f'paired queries: {done}/{total}', flush=True))
    if output is None:
        print(json.dumps(result, ensure_ascii=False))
    else:
        with output.open('x', encoding='utf-8') as stream:
            output.chmod(0o600)
            json.dump(result, stream, ensure_ascii=False, indent=2)
        print(json.dumps({'output': str(output), 'queries': len(result['results']), 'release_authorized': False}))
