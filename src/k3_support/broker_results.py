"""Accept immutable worker reports; acceptance is not execution or repair proof."""

import hashlib
from datetime import UTC, datetime

from .broker_receipts import execute
from .ids import canonical_json


def submit(conn, request, *, peer_uid, now=None):
    from .executors import ExecutorError, validate_codex_result_text

    now = now or datetime.now(UTC)
    if request.get("method") != "result":
        raise ValueError("result request required")

    def handler(db, binding, validated):
        report = validated["params"]["result"]
        try:
            sections = validate_codex_result_text(report)
            result_digest = hashlib.sha256(report.encode("utf-8")).hexdigest()
        except (ExecutorError, UnicodeError) as error:
            raise ValueError("invalid worker report") from error
        prior = db.execute("SELECT result_digest FROM broker_results WHERE grant_id=?",
                           (binding["grant_id"],)).fetchone()
        if prior is not None:
            if prior["result_digest"] != result_digest:
                raise ValueError("attempt result already submitted")
        else:
            db.execute("""INSERT INTO broker_results
                       (grant_id,job_id,attempt_no,lifecycle_round,input_digest,result_digest,
                        result_text,sections_json,received_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                       (binding["grant_id"], binding["job_id"], binding["attempt_no"],
                        binding["lifecycle_round"], binding["input_digest"], result_digest,
                        report, canonical_json(sections), now.isoformat()))
        # No Case/job state, board lease, process-exit or outbound mutation here.
        return {"accepted": True, "job_id": binding["job_id"]}

    return execute(conn, request, peer_uid=peer_uid, handler=handler, now=now)
