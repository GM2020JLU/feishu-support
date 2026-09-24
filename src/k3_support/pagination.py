"""Pure strict pagination decoding; provider and business policy stay at callers."""

from dataclasses import dataclass
from typing import Any


class PaginationError(ValueError):
    pass


_MISSING = object()


@dataclass(frozen=True)
class Page:
    messages: tuple[dict[str, Any], ...]
    has_more: bool
    next_token: str | None


def decode_page(data, *, meta=_MISSING, current_token=None, allow_empty_more=False):
    if not isinstance(data, dict) or not isinstance(data.get('messages'), list):
        raise PaginationError('page has no messages list')
    messages = data['messages']
    ids = []
    for item in messages:
        if (not isinstance(item, dict) or not isinstance(item.get('message_id'), str)
                or not item['message_id'].strip()):
            raise PaginationError('page has invalid message IDs')
        ids.append(item['message_id'])
    if len(set(ids)) != len(ids):
        raise PaginationError('page repeats message IDs')
    states, tokens = [], []
    if 'has_more' in data:
        if type(data['has_more']) is not bool:
            raise PaginationError('page has no explicit has_more boolean')
        states.append(data['has_more'])
    for key in ('page_token', 'next_page_token'):
        if key in data:
            tokens.append(data[key])
    for metadata in (meta, data.get('meta', _MISSING)):
        if metadata is _MISSING:
            continue
        if not isinstance(metadata, dict):
            raise PaginationError('page metadata is not an object')
        if 'pagination' not in metadata:
            continue
        pagination = metadata['pagination']
        if not isinstance(pagination, dict):
            raise PaginationError('pagination metadata is not an object')
        if 'complete' in pagination:
            if type(pagination['complete']) is not bool:
                raise PaginationError('pagination complete must be a boolean')
            states.append(not pagination['complete'])
        if 'next_token' in pagination:
            tokens.append(pagination['next_token'])
    if not states:
        raise PaginationError('page has no explicit completion evidence')
    if len(set(states)) != 1:
        raise PaginationError('conflicting pagination completion evidence')
    for token in tokens:
        if token is not None and (not isinstance(token, str) or len(token) > 16384 or (token and not token.strip())):
            raise PaginationError('invalid pagination token type or size')
    normalized = {token or None for token in tokens}
    if len(normalized) > 1:
        raise PaginationError('conflicting pagination tokens')
    next_token = next(iter(normalized), None)
    more = states[0]
    if more and (not next_token or (not messages and not allow_empty_more)):
        raise PaginationError('has_more without a usable page token or page')
    if not more and next_token:
        raise PaginationError('complete page still has a continuation token')
    if more and next_token == current_token:
        raise PaginationError('pagination token repeated')
    return Page(tuple(messages), more, next_token)
