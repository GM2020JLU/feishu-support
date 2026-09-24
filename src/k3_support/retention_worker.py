"""Opt-in, separately scheduled unreferenced database-payload retention."""

from .body_retention import clear_unreferenced_page, preview
from .runtime_control import capability_allowed
from .retention_transaction import RetentionDeadline


class BodyRetentionWorker:
    def __init__(self):
        self.cursor = ''

    def tick_with_health(self, conn, config):
        from .operations import heartbeat
        result = self.tick(conn, config)
        status = 'degraded' if result.get('reason') == 'dependency_scan_incomplete' else 'ready'
        heartbeat(conn, 'body_retention', status, result)
        return result

    def tick(self, conn, config):
        from .retention_settings import snapshot
        policy = snapshot(conn, config)
        if policy['needs_migration']:
            return {'skipped': True, 'reason': 'policy_base_changed', 'cleared': 0}
        days = policy['days']
        if days is None or config.mode != 'active':
            return {'skipped': True, 'reason': 'disabled_or_not_active', 'cleared': 0}
        if type(days) is not int or not 1 <= days <= 3650:
            raise ValueError('invalid body retention policy')
        if not capability_allowed(conn, config, 'triage'):
            return {'skipped': True, 'reason': 'global_mode_hold', 'cleared': 0}
        from .retention_reference_maintenance import tick as maintain_references
        try:
            index = maintain_references(conn)
        except RetentionDeadline:
            return {'skipped': True, 'reason': 'dependency_scan_incomplete', 'cleared': 0,
                    'index_state': 'deadline_deferred'}
        except (ValueError, RetentionDeadline):
            return {'skipped': True, 'reason': 'dependency_scan_incomplete', 'cleared': 0,
                    'index_state': 'requires_recovery'}
        if not index['ready']:
            return {'skipped': True, 'reason': 'dependency_scan_incomplete', 'cleared': 0,
                    'index_progress': index}
        page = preview(conn, days=days, after_id=self.cursor, limit=10)
        expected = {item['event_pk']: item['snapshot_digest'] for item in page['items']
                    if not item['clear_blockers']}
        if page['json_scan_issues']:
            return {'skipped': True, 'reason': 'dependency_scan_incomplete', 'cleared': 0}
        result = {'cleared': 0, 'body_bytes': 0}
        try:
            if expected:
                result = clear_unreferenced_page(conn, days=days, expected=expected,
                    actor='policy:body-retention', after_id=self.cursor, limit=10,
                    guard=lambda: snapshot(conn, config) == policy and capability_allowed(conn, config, 'triage'))
        except ValueError:
            # A hot page must not starve every later event. No failed selection
            # is cleared; revisit it after the bounded keyset cycle wraps.
            self.cursor = page['next_cursor'] or ''
            return {'skipped': True, 'reason': 'preview_changed_or_target_retained', 'cleared': 0,
                    'examined': len(page['items']), 'scan_cycle_complete': not self.cursor}
        self.cursor = page['next_cursor'] or ''
        return {**result, 'examined': len(page['items']), 'scan_cycle_complete': not self.cursor,
                'scope': 'database_payload_only_not_files_backups_or_secure_erasure'}


def main():
    from .services import _run
    _run('body-retention', BodyRetentionWorker().tick_with_health, interval=60)
