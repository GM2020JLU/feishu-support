"""Build-time corpus snapshots; generation freshness is not release approval."""

from .db import transaction
import json

from .ids import digest, canonical_json
from .knowledge_runtime import corpus_rows, corpus_fingerprint, entry_fingerprint, query_tokens, metadata_text
from .timeutil import iso_now


SOURCE_TABLES = (
    'knowledge_sources', 'source_registry', 'professional_knowledge_revisions',
    'professional_knowledge_claims', 'professional_claim_sources',
    'professional_validation_runs', 'professional_knowledge_publications',
    'professional_source_change_events',
)

METADATA_FIELDS = ('knowledge_id', 'title', 'status', 'question_variants_json',
    'project', 'module', 'hardware', 'software_version', 'applicability',
    'disclosure_class', 'allowed_chat_ids_json', 'allowed_user_ids_json',
    'confidence', 'source_authority', 'review_due_at', 'source_digest',
    'content_digest', 'professional_revision_id', 'revision_state', 'revision_digest')


def metadata_projection(row):
    result = {key: row.get(key) for key in METADATA_FIELDS}
    payload = json.loads(row['revision_payload']) if row.get('revision_payload') else {}
    # Keep only fields consumed by existing eligibility/scope logic. Claims,
    # procedures, code examples and revision body do not enter the projection.
    result['revision_payload'] = canonical_json({
        'kind': payload.get('kind', 'legacy'), 'scope': payload.get('scope', {}),
        'intent': {key: payload.get('intent', {}).get(key, [])
                   for key in ('required_entities', 'negative_constraints')},
        'sources': [{'share_mode': source.get('share_mode')} for source in payload.get('sources', [])],
    })
    return result


def metadata_page(conn, *, after_id='', limit=100):
    if type(limit) is not int or not 1 <= limit <= 500 or not isinstance(after_id, str):
        raise ValueError('invalid metadata page')
    with transaction(conn, immediate=False):
        current = state(conn)
        if not current['current']:
            raise ValueError('corpus metadata is not current')
        records = conn.execute('''SELECT knowledge_id,entry_digest,metadata_json
            FROM knowledge_corpus_metadata WHERE revision=? AND knowledge_id>?
            ORDER BY knowledge_id LIMIT ?''', (current['revision'], after_id, limit+1)).fetchall()
        return {'revision': current['revision'], 'corpus_digest': current['corpus_digest'],
                'sources_digest': current['sources_digest'],
                'items': [{**json.loads(row['metadata_json']), 'entry_fingerprint': row['entry_digest']}
                          for row in records[:limit]],
                'next_cursor': records[limit-1]['knowledge_id'] if len(records)>limit else None}


def state(conn):
    row = conn.execute('SELECT * FROM knowledge_corpus_state WHERE singleton=1').fetchone()
    if row is None:
        raise ValueError('corpus generation state missing')
    return {**dict(row), 'current': row['built_revision'] == row['revision'],
            'release_authorized': False}


def build(conn):
    with transaction(conn, immediate=False):
        revision = state(conn)['revision']
        rows = corpus_rows(conn)
        # Build-only baseline. Never execute these full scans per user query.
        sources = {table: [dict(row) for row in conn.execute(f'SELECT * FROM {table}')]
                   for table in SOURCE_TABLES}
    corpus = corpus_fingerprint(rows)
    source_digest = digest({table: sorted(digest(row) for row in values)
                            for table, values in sources.items()})
    projected = [(revision, row['knowledge_id'], entry_fingerprint(row), canonical_json(metadata_projection(row)))
                 for row in rows]
    terms = []
    for row in rows:
        meta = set(query_tokens(metadata_text(row)))
        body = set(query_tokens(row['answer_markdown']))
        terms.extend((revision, token, row['knowledge_id'], 4 if token in meta else 1)
                     for token in sorted(meta | body))
    with transaction(conn):
        if state(conn)['revision'] != revision:
            return {'built': False, 'reason': 'corpus_changed', 'release_authorized': False}
        existing = conn.execute('SELECT * FROM knowledge_corpus_builds WHERE revision=?', (revision,)).fetchone()
        if existing and (existing['corpus_digest'], existing['sources_digest']) != (corpus, source_digest):
            raise ValueError('same corpus revision has conflicting content')
        conn.execute('''INSERT INTO knowledge_corpus_builds VALUES(?,?,?,?,?)
            ON CONFLICT(revision) DO NOTHING''', (revision, corpus, source_digest, len(rows), iso_now()))
        conn.executemany('''INSERT INTO knowledge_corpus_metadata VALUES(?,?,?,?)
            ON CONFLICT(revision,knowledge_id) DO NOTHING''', projected)
        conn.executemany('''INSERT INTO knowledge_corpus_terms VALUES(?,?,?,?)
            ON CONFLICT(revision,token,knowledge_id) DO NOTHING''', terms)
        conn.execute('''UPDATE knowledge_corpus_state SET built_revision=?,corpus_digest=?,sources_digest=?
            WHERE singleton=1 AND revision=?''', (revision, corpus, source_digest, revision))
    return {'built': True, 'revision': revision, 'corpus_digest': corpus,
            'sources_digest': source_digest, 'entry_count': len(rows), 'release_authorized': False}
