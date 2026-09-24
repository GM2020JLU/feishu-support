import pytest
from test_mail_calendar_base import configured

from k3_support import db
from k3_support.base_sync import sync_case
from k3_support.base_sync_protocol import BaseSyncBusy
from k3_support.store import create_case


def test_upgrade_preserves_unproven_historical_requests_and_old_binary_rejects(tmp_path, config, monkeypatch):
    files = db.migration_files()
    old = [item for item in files if item[0] <= 95]
    monkeypatch.setattr(db, 'migration_files', lambda: old)
    conn = db.connect(tmp_path / 'base-upgrade.db')
    try:
        db.migrate(conn)
        cfg = configured(config, base_sync=True)
        case, _ = create_case(conn, title='historical', case_type='bug', severity='P3', confidence=0.8)
        # Old schema has no transport phase or Base identity in mappings.
        conn.execute("""INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,attempt_no,lease_owner,
            available_at,created_at,updated_at,context_json) VALUES('old-base',?,'base_sync','running',?,1,
            'old-worker','fixture','fixture','fixture',json_object('entity_type','case','entity_id',?))""", (case, 'a'*64, case))
        before = [tuple(row) for row in conn.execute('SELECT * FROM jobs')]
        monkeypatch.setattr(db, 'migration_files', lambda: files)
        assert 96 in db.migrate(conn)
        assert [tuple(row) for row in conn.execute('SELECT * FROM jobs')] == before
        assert conn.execute('SELECT state FROM base_sync_legacy_holds').fetchone()[0] == 'unverified'
        assert conn.execute('SELECT count(*) FROM base_sync_operations').fetchone()[0] == 0
        with pytest.raises(BaseSyncBusy, match='migration inventory'):
            sync_case(conn, cfg, case_id=case, runner=lambda _: pytest.fail('unproven historical write'))
        monkeypatch.setattr(db, 'migration_files', lambda: old)
        with pytest.raises(db.DatabaseError, match='newer than this application'):
            db.migrate(conn)
    finally:
        conn.close()
