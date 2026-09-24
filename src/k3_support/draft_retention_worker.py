"""Default-off captured draft TTL, separate from raw attachments and body TTL."""
from .draft_retention import clear, preview
from .operations import heartbeat
from .runtime_control import capability_allowed
from .retention_settings import snapshot


class DraftRetentionWorker:
    def __init__(self):
        self.cursor = ''
        self.cycle_problem = False
        self.last_cycle_problem = False
        self.health_initialized = False

    def tick(self, conn, config):
        policy = snapshot(conn, config, scope='draft')
        if policy['needs_migration']:
            return {'cleared': 0, 'reason': 'policy_base_changed', 'needs_attention': True}
        days = policy['days']
        if days is None or config.mode != 'active':
            return {'cleared': 0, 'reason': 'disabled_or_not_active'}
        if type(days) is not int or not 1 <= days <= 3650:
            raise ValueError('invalid captured draft retention policy')
        if not capability_allowed(conn, config, 'triage'):
            return {'cleared': 0, 'reason': 'global_mode_hold'}
        # One candidate per tick bounds dependency scans; held candidates do not
        # prevent later IDs from being examined. Restart safely begins again.
        rows = conn.execute('SELECT candidate_id FROM knowledge_authoring_drafts '
                            'WHERE candidate_id>? ORDER BY candidate_id LIMIT 2', (self.cursor,)).fetchall()
        if not rows:
            self.cursor = ''
            return {'cleared': 0, 'reason': 'cycle_complete'}
        candidate = rows[0]['candidate_id']
        self.cursor = candidate if len(rows) > 1 else ''
        try:
            item = preview(conn, candidate_id=candidate, days=days)
            if not item['eligible']:
                return {'cleared': 0, 'reason': 'retained', 'blockers': item['blockers'],
                        'needs_attention': not item['scan_complete']}
            clear(conn, candidate_id=candidate, days=days, expected_digest=item['row_digest'],
                  actor_id='policy:captured-draft-retention', guard=lambda:
                  snapshot(conn, config, scope='draft') == policy
                  and config.mode == 'active' and capability_allowed(conn, config, 'triage'))
            return {'cleared': 1, 'logical_bytes': item['logical_bytes'],
                    'scope': 'unreviewed_captured_draft_only_no_files_or_backups'}
        except (ValueError, TypeError, KeyError):
            return {'cleared': 0, 'reason': 'changed_or_invalid', 'needs_attention': True}

    def tick_with_health(self, conn, config):
        if not self.health_initialized:
            prior = conn.execute("SELECT status FROM service_state WHERE component='draft_retention'").fetchone()
            self.last_cycle_problem = bool(prior and prior['status'] == 'degraded')
            self.health_initialized = True
        result = self.tick(conn, config)
        if result.get('reason') not in {'disabled_or_not_active', 'global_mode_hold'}:
            self.cycle_problem |= bool(result.get('needs_attention'))
            if not self.cursor:
                self.last_cycle_problem = self.cycle_problem
                self.cycle_problem = False
        # One good row is not proof the previous bad row was repaired. Keep the
        # condition stable until an entire subsequent scan completes cleanly.
        issue = self.cycle_problem or self.last_cycle_problem
        result['scan_cycle_needs_attention'] = issue
        heartbeat(conn, 'draft_retention', 'degraded' if issue else 'ready', result)
        return result


def main():
    from .services import _run
    _run('draft-retention', DraftRetentionWorker().tick_with_health, interval=60)
