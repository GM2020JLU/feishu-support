"""Read-only review draft: worker claims never become a repair verdict."""

import base64
import hashlib
import json
from contextlib import nullcontext

from .codex_result_source import read_result
from .db import transaction
from .executors import ExecutorError, validate_codex_result_text
from .project_bugs import _text

SECTION_LIMIT = 4000


def draft(conn, *, bug_id, job_id, config=None):
    _text(bug_id, "Bug ID")
    _text(job_id, "job ID")
    # One local snapshot; never open a worker file or execute a review on a GET.
    with nullcontext() if conn.in_transaction else transaction(conn, immediate=False):
        job = conn.execute(
            """SELECT j.*,r.round_id,r.archived_at,r.repair_state,r.verification_state,b.revision
               FROM jobs j JOIN project_investigation_jobs i USING(job_id)
               JOIN project_bug_rounds r USING(round_id)
               JOIN project_bugs b USING(bug_id)
               WHERE j.job_id=? AND b.bug_id=? AND j.case_id=b.case_id""",
            (job_id, bug_id),
        ).fetchone()
        if job is None:
            raise ValueError("investigation result unavailable for this Bug")
        result = {
            "bug_id": bug_id, "job_id": job_id, "round_id": job["round_id"],
            "attempt_no": job["attempt_no"],
            "archived": job["archived_at"] is not None,
            "execution_state": job["state"], "repair_state": job["repair_state"],
            "verification_state": job["verification_state"],
            "report": {"state": "not_received"}, "review": None,
            "writeback_available": False, "functional_verdict": "not_established",
            "evidence_limit": 5,
            "bug_revision": job["revision"],
        }
        from .project_repair_reviews import detail as repair_detail

        repair=repair_detail(conn,config,bug_id=bug_id,round_id=job['round_id'])
        result['repair_state']=repair['repair_state']
        result['repair_review_state']=repair['review_state']
        snapshot = conn.execute(
            "SELECT snapshot_id FROM project_bug_snapshots WHERE bug_id=? ORDER BY sequence DESC LIMIT 1",
            (bug_id,),
        ).fetchone()
        result["snapshot_id"] = snapshot[0] if snapshot else None
        from .project_verification import current

        plan = current(conn, job["round_id"])
        if plan is not None:
            result["verification_state"] = plan["verification_state"]
            result["verification_source"] = plan["operator_verification"]["verdict_source"]
            if plan["verification_state"] == "passed":
                result["functional_verdict"] = "operator_reviewed_pass"
        report = conn.execute(
            "SELECT received_at FROM broker_results WHERE job_id=? AND attempt_no=?",
            (job_id, job["attempt_no"]),
        ).fetchone()
        if report is not None:
            # Explicitly disallow read_result's legacy file fallback, including
            # inconsistent DBs where the report survives without a broker grant.
            broker = conn.execute("SELECT 1 FROM broker_grants WHERE job_id=?", (job_id,)).fetchone()
            try:
                if broker is None:
                    raise ValueError("missing broker binding")
                raw = read_result(conn, job_id=job_id)
                sections = validate_codex_result_text(raw.decode("utf-8"))
            except (ValueError, ExecutorError, UnicodeError):
                result["report"] = {"state": "unavailable"}
            else:
                digest = hashlib.sha256(raw).hexdigest()
                result["report"] = {
                    "state": "available", "source": "worker_claim", "digest": digest,
                    "received_at": report["received_at"],
                    "sections": {key: text[:SECTION_LIMIT] for key, text in sections.items()},
                    "truncated_sections": [key for key, text in sections.items() if len(text) > SECTION_LIMIT],
                }
                # Validate full bytes, not the truncated display. This is only a
                # format check; never execute commands or create a review on GET.
                from .review import ReviewError, validate_codex_manifest

                try:
                    context = json.loads(job["context_json"])
                    validate_codex_manifest(
                        sections["artifacts"], case_id=job["case_id"],
                        configured_repositories=set(context.get("repositories") or []),
                        remote_worktree_root=config.runtime("remote_worktree_root") if config else None,
                    )
                except (ReviewError, ValueError, TypeError, KeyError):
                    result["report"]["artifact_format"] = "invalid"
                else:
                    result["report"]["artifact_format"] = "valid"
                review = conn.execute(
                    """SELECT review_id,status,result_digest FROM codex_reviews
                       WHERE job_id=? AND case_id=?""", (job_id, job["case_id"]),
                ).fetchone()
                if review is not None:
                    result["review"] = {
                        "review_id": review["review_id"],
                        "state": review["status"] if review["result_digest"] == digest else "stale",
                        "functional_verdict": "not_established",
                    }
        from .project_investigation_after import projection as after
        from .project_investigation_checkout import projection as before

        result["checkout_before"] = before(conn, job_id)
        result["checkout_after"] = after(conn, job_id)
        for observation in result['checkout_after']:
            changeset = observation.get('changeset')
            if changeset and changeset.get('state')=='observed':
                # Display immutable independently collected bytes as text only.
                # Binary/non-UTF8 patches retain the original base64 and digest.
                changeset['patch_text'] = base64.b64decode(changeset['patch_b64']).decode('utf-8','replace')
                for key,target in [('paths_b64','paths'),('untracked_paths_b64','untracked_paths')]:
                    changeset[target]=[p.decode('utf-8','backslashreplace') for p in base64.b64decode(changeset[key]).split(b'\0') if p]
        # A receipt is process evidence, never evidence of the model's test claim.
        result["commands"] = [dict(row) for row in conn.execute(
            """SELECT a.request_id,g.attempt_no,a.state,r.exit_code,r.finished_at
               FROM broker_remote_actions a JOIN broker_grants g USING(grant_id)
               LEFT JOIN broker_remote_results r USING(request_id)
               WHERE g.job_id=? ORDER BY a.created_at DESC,a.request_id DESC LIMIT 5""",
            (job_id,),
        )]
        return result
