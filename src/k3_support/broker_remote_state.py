"""Shared unsettled predicate for trusted completion and read-only presentation.

Queries must bind aliases a=broker_remote_actions, r=broker_remote_results.
Reserved guardian/SSH failures cannot serve as a normal command exit receipt.
"""

UNSETTLED = """((a.state IN ('running','unknown') OR
    (a.state IN ('queued','cancelled') AND r.request_id IS NOT NULL) OR
    (a.state IN ('succeeded','failed') AND (r.request_id IS NULL OR NOT
      ((a.state='succeeded' AND r.exit_code=0) OR
       (a.state='failed' AND r.exit_code BETWEEN 1 AND 254 AND r.exit_code NOT IN (124,125))))))
    AND NOT EXISTS(SELECT 1 FROM broker_remote_cleanup k JOIN broker_remote_observations o
      ON o.observation_id=k.observation_id WHERE k.request_id=a.request_id AND o.request_id=a.request_id
      AND k.observation_snapshot=o.snapshot_digest AND k.observation_result=o.result_json
      AND o.state='observed' AND k.grant_id=a.grant_id AND k.request_digest=a.request_digest
      AND k.plan_json=a.plan_json AND k.action_state=a.state AND k.action_updated_at=a.updated_at
      AND k.result_exit_code IS r.exit_code))"""
