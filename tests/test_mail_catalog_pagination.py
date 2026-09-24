import json

import pytest
from test_mail_catalog import _classifier

from k3_support.db import connect
from k3_support.lark import CommandResult
from k3_support.mail_catalog import MailCatalogError, MailCatalogStale, scan_mail_catalog


def scan(conn, config, runner, **kwargs):
    config.raw['features']['mail'] = True
    return scan_mail_catalog(conn, config, runner=runner, classifier=_classifier, max_pages=1, **kwargs)


def page(token=None, more=False):
    return CommandResult({'messages': [], 'has_more': more, 'page_token': token}, 'user', [])


def test_token_cycle_is_detected_across_calls_and_reopened_connections(conn, config):
    scan(conn, config, lambda _: page('A', True))
    reopened = connect(config.database_path)
    try:
        scan(reopened, config, lambda _: page('B', True))
        before = tuple(reopened.execute('SELECT folder_index,next_page_token,pages_processed FROM mail_catalog_runs').fetchone())
        with pytest.raises(MailCatalogError, match='cycle'):
            scan(reopened, config, lambda _: page('A', True))
        assert tuple(reopened.execute('SELECT folder_index,next_page_token,pages_processed FROM mail_catalog_runs').fetchone()) == before
        run = reopened.execute('SELECT * FROM mail_catalog_runs').fetchone()
        assert run['state'] == 'failed'
        assert json.loads(run['error_json'])['reason'] == 'pagination_protocol_error'
        def forbidden(_):
            pytest.fail('failed scans must not issue requests')
        assert scan(reopened, config, forbidden)['resume_required']
        resumed = scan(reopened, config, lambda argv: page(), resume_failed=True)
        assert resumed['run_id'] == run['run_id'] and resumed['folder_index'] == 1
    finally:
        reopened.close()


@pytest.mark.parametrize('data,meta', [
    ({'messages': []}, {}),
    ({'messages': [], 'has_more': 'false'}, {}),
    ({'messages': [], 'has_more': False}, {'pagination': {'complete': False}}),
    ({'messages': [], 'has_more': True, 'page_token': 12}, {}),
    ({'messages': [], 'has_more': False, 'page_token': 'not-finished'}, {}),
])
def test_bad_page_commits_no_items_or_cursor(conn, config, data, meta):
    with pytest.raises(MailCatalogError):
        scan(conn, config, lambda _: CommandResult(data, 'user', [], meta))
    run = conn.execute('SELECT * FROM mail_catalog_runs').fetchone()
    assert run['state'] == 'failed' and run['pages_processed'] == 0
    assert run['next_page_token'] is None
    assert conn.execute('SELECT count(*) FROM mail_catalog_cursors').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM mail_catalog_items').fetchone()[0] == 0


def test_late_page_cannot_overwrite_winning_checkpoint_or_mark_it_failed(conn, config):
    other = connect(config.database_path)
    try:
        def delayed(_):
            won = scan(other, config, lambda _: page('winner', True))
            assert won['pages_processed'] == 1
            return page('loser', True)
        with pytest.raises(MailCatalogStale):
            scan(conn, config, delayed)
        row = conn.execute('SELECT * FROM mail_catalog_runs').fetchone()
        assert row['state'] == 'running'
        assert row['next_page_token'] == 'winner' and row['pages_processed'] == 1
    finally:
        other.close()


def test_page_budget_failure_retains_cursor_and_requires_explicit_recovery(conn, config):
    scan(conn, config, lambda _: page('next', True))
    conn.execute('UPDATE mail_catalog_runs SET page_limit=1')
    with pytest.raises(MailCatalogError, match='budget'):
        scan(conn, config, lambda _: pytest.fail('budget must prevent network'))
    row = conn.execute('SELECT * FROM mail_catalog_runs').fetchone()
    assert row['next_page_token'] == 'next' and row['state'] == 'failed'
    assert json.loads(row['error_json'])['reason'] == 'page_budget_exhausted'


def test_explicit_restart_fences_the_previous_inflight_page(conn, config):
    other = connect(config.database_path)
    try:
        def delayed(_):
            scan(other, config, lambda _: page('new', True), restart=True)
            return page('obsolete', True)
        with pytest.raises(MailCatalogStale):
            scan(conn, config, delayed)
        rows = conn.execute('SELECT state,next_page_token FROM mail_catalog_runs ORDER BY rowid').fetchall()
        assert [tuple(row) for row in rows] == [('failed', None), ('running', 'new')]
    finally:
        other.close()


def test_meta_only_complete_does_not_require_provider_token_format(conn, config):
    result = scan(conn, config, lambda _: CommandResult({'messages': []}, 'user', [], {'pagination': {'complete': True}}))
    assert result['folder_index'] == 1


def test_cursor_cleanup_is_bounded_and_preserves_resumable_failed_runs(conn, config):
    from k3_support.mail_catalog import prune_cursor_history
    scan(conn, config, lambda _: page('A', True))
    scan(conn, config, lambda _: page('B', True))
    conn.execute("UPDATE mail_catalog_runs SET state='failed',last_error='transport',updated_at='2000-01-01T00:00:00+00:00'")
    assert prune_cursor_history(conn, limit=1) == 0
    conn.execute("UPDATE mail_catalog_runs SET last_error='explicit_restart'")
    assert prune_cursor_history(conn, limit=1) == 1
    assert conn.execute('SELECT count(*) FROM mail_catalog_cursors').fetchone()[0] == 1
    assert prune_cursor_history(conn, limit=1) == 1
