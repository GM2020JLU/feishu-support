"""Creation duplicate candidates come from owned, current Project searches."""
import json

from . import project_bug_create as drafts
from . import project_bug_search as search
from .project_bug_query import validate_spaces
from .timeutil import parse_iso, utc_now


def _scope(conn, config, actor, draft):
    reader = search._reader(conn, config, actor)
    if reader['host'] != draft['host']:
        raise PermissionError('duplicate search host does not match draft')
    spaces = validate_spaces(config.raw.get('project_integration', {}).get('search_spaces', []))
    matches = [s for s in spaces if draft['project_key'] in {s['project_key'], s['simple_name']}
               and draft['type_key'] in {s['type_key'], s.get('url_type_key', s['type_key'])}]
    if len(matches) != 1 or matches[0]['allowed_item_ids'] is not None:
        raise PermissionError('duplicate search needs an explicit whole-type read scope')
    return reader, matches[0]


def _draft(conn, actor, draft_id, expected_digest):
    row = drafts._owned(conn, draft_id, actor)
    drafts._expect(row, expected_digest)
    drafts._require_grant(conn, row)
    if row['state'] not in drafts.DRAFT_STATES:
        raise ValueError('create draft is not editable')
    return row


def enqueue(conn, config, *, actor, draft_id, expected_digest, keyword, request_id):
    row = _draft(conn, actor, draft_id, expected_digest)
    _, scope = _scope(conn, config, actor, row)
    if not isinstance(keyword, str) or not keyword.strip():
        raise ValueError('provide a duplicate search keyword')
    return search.enqueue(conn, config, actor=actor, simple_name=scope['simple_name'],
                          type_key=scope['type_key'], keyword=keyword, request_id=request_id)


def _observed(conn, config, actor, draft, search_id):
    reader, scope = _scope(conn, config, actor, draft)
    row = search._get(conn, actor, search_id)
    if (row['state'] != 'succeeded' or row['after_id'] != 0
            or parse_iso(row['expires_at']) <= utc_now()
            or json.loads(row['scope_json']) != scope
            or row['reader_digest'] != search._stamp(conn, config, reader, scope)):
        raise ValueError('duplicate search is incomplete, expired or outside current scope')
    result = json.loads(row['result_json'])
    if result.get('host') != draft['host'] or result.get('next_after_id') is not None:
        raise ValueError('duplicate results need a narrower keyword before confirmation')
    actual = [{'item_id': r['item_id'], 'title': r['title']} for r in result['items']]
    return actual


def require_current(conn, config, draft):
    if not draft['duplicate_search_id'] or not draft['duplicate_confirmed_at']:
        raise ValueError('duplicate search must be observed and confirmed')
    actual = _observed(conn, config, draft['actor'], draft, draft['duplicate_search_id'])
    if draft['duplicate_candidates_json'] is None or json.loads(draft['duplicate_candidates_json']) != actual:
        raise ValueError('confirmed duplicate candidates no longer match observed results')


def attach(conn, config, *, actor, draft_id, expected_digest, search_id, candidates=None):
    draft = _draft(conn, actor, draft_id, expected_digest)
    actual = _observed(conn, config, actor, draft, search_id)
    if candidates is not None and candidates != actual:
        raise ValueError('supplied duplicate candidates differ from observed results')
    return drafts.attach_duplicates(conn, actor=actor, draft_id=draft_id,
                                    search_id=search_id, candidates=actual)
