"""Control-side, single-slot worker launch with durable no-retry semantics."""

import subprocess
from datetime import UTC, datetime
from uuid import UUID, uuid4

from .broker_catalog import ActiveCatalog
from .broker_completion import reconcile
from .broker_execution_contract import ExecutionContract
from .broker_policy import execution_allowed
from .broker_queue import next_candidate
from .broker_resources import inspect_settlement
from .db import transaction
from .timeutil import iso_now


def start_unit(claim_request_id):
    # The identifier is generated locally, not accepted from messages or workers.
    if not isinstance(claim_request_id, str) or str(UUID(claim_request_id)) != claim_request_id:
        raise ValueError("canonical launch identifier required")
    result = subprocess.run(["/usr/bin/systemctl", "--system", "--no-ask-password", "start", "--no-block",
                             f"k3-support-broker-worker@{claim_request_id}.service"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, timeout=5, check=False)
    return result.returncode == 0


def dispatch_one(conn, config, *, contract_reader, launch=start_unit):
    contracts = ([p.contract for p in contract_reader.catalog.profiles()]
                 if type(contract_reader) is ActiveCatalog else [contract_reader()])
    if not contracts or any(type(contract) is not ExecutionContract for contract in contracts):
        raise ValueError("trusted launch contract required")
    with transaction(conn):
        contracts = [contract for contract in contracts if execution_allowed(conn, config, contract=contract)]
        if not contracts:
            return {"state": "disabled"}
        # Lost responses, crashes and rejected launches all need reconciliation.
        # Never create a fresh UUID merely because the previous launch is old.
        if conn.execute("SELECT 1 FROM broker_launches WHERE state!='finished'").fetchone():
            return {"state": "occupied"}
        if conn.execute("""SELECT 1 FROM jobs j WHERE job_type='codex' AND state='running'
            AND NOT EXISTS (SELECT 1 FROM broker_execution_resources r
                WHERE r.job_id=j.job_id AND r.attempt_no=j.attempt_no AND r.settled_at IS NOT NULL)""").fetchone():
            return {"state": "occupied"}
        candidates = []
        now = datetime.now(UTC)
        for contract in contracts:
            row = next_candidate(conn, now=now, contract=contract,
                                 require_selection=type(contract_reader) is ActiveCatalog)
            if row is not None:
                candidates.append((row, contract))
        if not candidates:
            return {"state": "idle"}
        eligible, contract = min(candidates, key=lambda pair: (
            pair[0]["priority"], pair[0]["created_at"], pair[0]["job_id"]))
        request_id = str(uuid4())
        stamp = iso_now()
        conn.execute("INSERT INTO broker_launches VALUES(?,'launching',?,?)", (request_id, stamp, stamp))
        conn.execute("INSERT INTO broker_launch_bindings VALUES(?,?,?,?,?)",
                     (request_id, eligible["job_id"], eligible["input_digest"], contract.agent, contract.fingerprint))
    # A committed intent precedes the external request. Neither a timeout nor
    # systemctl success proves the coding process started or stopped.
    accepted = False
    try:
        accepted = launch(request_id) is True
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        with transaction(conn):
            conn.execute("UPDATE broker_launches SET state=?,updated_at=? WHERE claim_request_id=? AND state='launching'",
                         ("accepted" if accepted else "unknown", iso_now(), request_id))
    return {"state": "accepted" if accepted else "unknown", "claim_request_id": request_id,
            "execution_verified": False}


def finish_observed(conn):
    """Release physical resources independently of the current business round."""
    candidates = conn.execute(
        "SELECT l.claim_request_id,i.grant_id FROM broker_launches l "
        "JOIN broker_execution_instances i USING(claim_request_id) "
        "JOIN broker_service_exits e ON e.grant_id=i.grant_id AND e.invocation_id=i.invocation_id "
        "WHERE l.state!='finished' ORDER BY l.created_at,l.claim_request_id LIMIT 4").fetchall()
    finished = 0
    for candidate in candidates:
        with transaction(conn):
            if not conn.execute("SELECT 1 FROM broker_launches WHERE claim_request_id=? AND state!='finished'",
                                (candidate['claim_request_id'],)).fetchone():
                continue
            settlement = inspect_settlement(conn, grant_id=candidate['grant_id'])
            if settlement.state != 'settled':
                continue
            conn.execute('UPDATE broker_execution_resources SET settled_at=? WHERE grant_id=? AND settled_at IS NULL',
                         (iso_now(), candidate['grant_id']))
            finished += conn.execute(
                "UPDATE broker_launches SET state='finished',updated_at=? WHERE claim_request_id=? AND state!='finished'",
                (iso_now(), candidate["claim_request_id"])).rowcount
        # Report recording is recoverable through the observer's independent
        # sweep, even if it fails after resource release has committed.
        reconcile(conn, grant_id=candidate['grant_id'])
    return {"finished": finished}


def reclaim_unstarted_launches(conn):
    """Requeue a claimed-but-never-started job once its lease expires.

    A worker that dies between claim and start (crash, rejected input
    projection, lost socket) leaves the launch unfinished and the dispatcher
    permanently occupied. Without a start authorization no execution was
    ever permitted, so after lease expiry the job can safely return to the
    queue and the launch can settle. Anything with a start record stays with
    the exit-observation path.
    """
    rows = conn.execute("""
        SELECT l.claim_request_id,b.job_id FROM broker_launches l
        JOIN broker_launch_bindings b USING(claim_request_id)
        JOIN jobs j ON j.job_id=b.job_id
        WHERE l.state!='finished' AND j.state='running'
          AND j.input_digest=b.input_digest
          AND j.lease_expires_at IS NOT NULL AND j.lease_expires_at<?
          AND NOT EXISTS (SELECT 1 FROM broker_execution_starts s
                          WHERE s.job_id=j.job_id AND s.attempt_no=j.attempt_no)
        ORDER BY l.created_at LIMIT 4""", (iso_now(),)).fetchall()
    reclaimed = 0
    for row in rows:
        with transaction(conn):
            live = conn.execute("""
                SELECT 1 FROM broker_launches l JOIN jobs j ON j.job_id=?
                WHERE l.claim_request_id=? AND l.state!='finished'
                  AND j.state='running' AND j.lease_expires_at<?
                  AND NOT EXISTS (SELECT 1 FROM broker_execution_starts s
                                  WHERE s.job_id=j.job_id AND s.attempt_no=j.attempt_no)""",
                (row["job_id"], row["claim_request_id"], iso_now())).fetchone()
            if not live:
                continue
            conn.execute(
                "UPDATE jobs SET state='queued',lease_owner=NULL,lease_expires_at=NULL,"
                "heartbeat_at=NULL,updated_at=? WHERE job_id=?",
                (iso_now(), row["job_id"]))
            conn.execute(
                "UPDATE broker_launches SET state='finished',updated_at=? WHERE claim_request_id=?",
                (iso_now(), row["claim_request_id"]))
            reclaimed += 1
    return {"reclaimed": reclaimed}
