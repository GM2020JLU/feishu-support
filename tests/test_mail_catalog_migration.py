import pytest
from test_mail_catalog_pagination import scan, page

from k3_support import db
from k3_support.ids import digest


def test_catalog_upgrade_backfills_only_known_checkpoint_and_rejects_old_binary(tmp_path, config, monkeypatch):
    files = db.migration_files()
    old = [item for item in files if item[0] <= 96]
    monkeypatch.setattr(db, 'migration_files', lambda: old)
    conn = db.connect(tmp_path / 'catalog-upgrade.db')
    try:
        db.migrate(conn)
        conn.execute("""INSERT INTO mail_catalog_runs(run_id,folders_json,folder_index,next_page_token,
            pages_processed,state,created_at,updated_at) VALUES('old-run','["INBOX","ARCHIVED"]',0,
            'known-current',20,'running','fixture','fixture')""")
        before = tuple(conn.execute('SELECT run_id,next_page_token,pages_processed FROM mail_catalog_runs').fetchone())
        monkeypatch.setattr(db, 'migration_files', lambda: files)
        assert 97 in db.migrate(conn)
        assert tuple(conn.execute('SELECT run_id,next_page_token,pages_processed FROM mail_catalog_runs').fetchone()) == before
        assert conn.execute('SELECT count(*) FROM mail_catalog_cursors').fetchone()[0] == 0
        result = scan(conn, config, lambda _: page('new-next', True))
        assert result['pages_processed'] == 21
        assert [row[0] for row in conn.execute('SELECT token_digest FROM mail_catalog_cursors')] == [digest('known-current')]
        monkeypatch.setattr(db, 'migration_files', lambda: old)
        with pytest.raises(db.DatabaseError, match='newer than this application'):
            db.migrate(conn)
    finally:
        conn.close()


def test_page_checkpoint_failure_rolls_back_history_and_content(conn, config, monkeypatch):
    from k3_support import mail_catalog
    from k3_support.lark import CommandResult
    original = mail_catalog._store_item
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('injected page commit interruption')
    monkeypatch.setattr(mail_catalog, '_store_item', interrupted)
    def runner(argv):
        data = {'messages': [{'message_id': 'synthetic', 'subject': 'Build successful'}]}
        if '+triage' in argv:
            data.update(has_more=True, page_token='next')
        return CommandResult(data, 'user', [])
    with pytest.raises(RuntimeError, match='interruption'):
        scan(conn, config, runner)
    assert conn.execute('SELECT count(*) FROM mail_catalog_items').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM mail_catalog_cursors').fetchone()[0] == 0
    assert tuple(conn.execute('SELECT state,pages_processed,next_page_token FROM mail_catalog_runs').fetchone()) == ('failed', 0, None)
