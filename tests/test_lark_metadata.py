"""Envelope metadata must survive the local CLI boundary without changing data."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from k3_support import lark


@pytest.mark.parametrize('runner', [lark.run_json, lark.run_mail_json])
def test_pagination_envelope_is_not_lost(monkeypatch, runner):
    value = {'ok': True, 'identity': 'user', 'data': {'messages': []},
             'meta': {'pagination': {'complete': False, 'next_token': 'opaque-token'}}}
    monkeypatch.setattr(lark, '_capture', lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr=''))
    result = runner(['im', '+threads-messages-list', '--as', 'user'])
    assert result.data == {'messages': []}
    assert result.meta == value['meta']
    assert result.identity == 'user'


@pytest.mark.parametrize('runner', [lark.run_json, lark.run_mail_json])
def test_invalid_metadata_is_not_silently_replaced_by_success(monkeypatch, runner):
    value = {'ok': True, 'identity': 'user', 'data': {}, 'meta': 'complete'}
    monkeypatch.setattr(lark, '_capture', lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr=''))
    with pytest.raises(lark.LarkError, match='metadata'):
        runner(['im', '+threads-messages-list', '--as', 'user'])
