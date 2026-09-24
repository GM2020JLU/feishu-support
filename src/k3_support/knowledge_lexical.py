"""Bounded FTS metadata recall, independent of vector recall and body loading."""

import json
from collections import Counter

from .db import transaction
from .knowledge_corpus import state
from .knowledge_runtime import (
    _INTERNAL_RELATIONS,
    _profile,
    eligible_rows,
    query_tokens,
)


def recall(conn, *, query, requester_id=None, chat_id=None, scope=None,
           verified_profile=None, facts=None, limit=80, metadata_budget=2000):
    if (type(limit) is not int or not 1 <= limit <= 500 or type(metadata_budget) is not int
            or not limit <= metadata_budget <= 10000):
        raise ValueError('invalid lexical metadata budget')
    if not isinstance(query, str) or len(query) > 32768:
        raise ValueError('invalid lexical query')
    tokens = query_tokens(query)
    expression = ' OR '.join('"'+token.replace('"', '""')+'"' for token in tokens[:128])
    with transaction(conn, immediate=False):
        current = state(conn)
        if not current['current']:
            raise ValueError('corpus metadata is not current')
        profile = _profile(conn, requester_id, verified_profile)
        confidence = profile.get('relationship_confidence')
        internal = (profile.get('relationship') in _INTERNAL_RELATIONS
                    and type(confidence) in (int, float) and 0.85 <= confidence <= 1)
        found, scanned, offset, complete = [], 0, 0, True
        filter_counts = Counter()
        while expression and len(found) < limit:
            remaining = metadata_budget-scanned
            if remaining == 0:
                complete = False
                break
            count = min(80, remaining)
            records = conn.execute('''WITH recalled AS (
                SELECT knowledge_id,bm25(knowledge_fts) AS rank FROM knowledge_fts WHERE knowledge_fts MATCH ?
                UNION ALL
                SELECT knowledge_id,1.0/(1+sum(weight)) AS rank FROM knowledge_corpus_terms
                WHERE revision=? AND token IN (SELECT value FROM json_each(?)) GROUP BY knowledge_id
                ), ranked AS (SELECT knowledge_id,min(rank) AS rank FROM recalled GROUP BY knowledge_id)
                SELECT m.metadata_json,m.entry_digest,ranked.rank
                FROM ranked JOIN knowledge_corpus_metadata m
                  ON m.knowledge_id=ranked.knowledge_id AND m.revision=?
                WHERE (
                  json_extract(m.metadata_json,'$.disclosure_class')='public'
                  OR (json_extract(m.metadata_json,'$.disclosure_class')='internal' AND ?)
                  OR (json_extract(m.metadata_json,'$.disclosure_class') IN ('team','private','restricted')
                      AND ? IS NOT NULL AND ? IN (SELECT value FROM json_each(json_extract(m.metadata_json,'$.allowed_chat_ids_json'))))
                  OR (json_extract(m.metadata_json,'$.disclosure_class') IN ('private','restricted')
                      AND ? IS NOT NULL AND ? IN (SELECT value FROM json_each(json_extract(m.metadata_json,'$.allowed_user_ids_json'))))
                ) ORDER BY rank,m.knowledge_id LIMIT ? OFFSET ?''',
                (expression, current['revision'], json.dumps(tokens[:128]), current['revision'], int(internal), chat_id, chat_id,
                 requester_id, requester_id, count, offset)).fetchall()
            scanned += len(records)
            offset += len(records)
            metadata = [{**json.loads(row['metadata_json']), 'entry_fingerprint': row['entry_digest']}
                        for row in records]
            eligible, rejected = eligible_rows(conn, metadata, requester_id=requester_id, chat_id=chat_id,
                                        scope=scope or {}, verified_profile=verified_profile, facts=facts)
            filter_counts.update(rejected)
            found.extend(eligible[:limit-len(found)])
            if len(records) < count:
                break
        return {'items': found, 'complete': complete, 'metadata_scanned': scanned,
                'filter_counts': dict(filter_counts),
                'reason': None if complete else 'metadata_budget_exhausted',
                'revision': current['revision'], 'corpus_digest': current['corpus_digest']}
