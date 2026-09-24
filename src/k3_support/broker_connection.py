"""Single authenticated connection dispatch; not a production listener."""

import sqlite3

from .broker_claim_receipts import claim
from .broker_input import read
from .broker_renew import renew
from .broker_results import submit
from .broker_start import authorize
from .broker_systemd_observer import observe_running
from .broker_transport import receive_authenticated_request, send_response
from .model_budget import BudgetError


def serve_connection(conn, sock, *, worker_uid, timeout=5.0, now=None, config=None, control_key=None,
                     instance_observer=None, contract_reader=None):
    """Own and close sock. Malformed/unauthenticated peers receive no response.

    Database commits precede response delivery; callers must never retry business
    execution on a send failure. Clients may replay the same request ID.
    """
    with sock:
        peer, request = receive_authenticated_request(sock, worker_uid=worker_uid, timeout=timeout)
        result = None
        error_code = None
        handler = {"renew": renew, "result": submit, "input": read}.get(request["method"])
        if request["method"] == "renew" and config is not None:
            handler = lambda db, value, **kw: renew(db, value, config=config, contract_reader=contract_reader, **kw)
        if request["method"] == "claim" and config is not None and control_key is not None:
            handler = lambda db, value, **kw: claim(db, config, value, control_key=control_key, contract_reader=contract_reader, **kw)
        if request["method"] == "start" and config is not None:
            handler = lambda db, value, **kw: authorize(db, config, value, observe_instance=instance_observer or observe_running, contract_reader=contract_reader, **kw)
        if request["method"] == "remote_submit" and config is not None:
            from .broker_remote import submit as submit_remote

            handler = lambda db, value, **kw: submit_remote(db, config, value, contract_reader=contract_reader, **kw)
        if request["method"] == "board_submit" and config is not None:
            from .broker_board import submit as submit_board

            handler = lambda db, value, **kw: submit_board(db, config, value, contract_reader=contract_reader, **kw)
        if request["method"] == "board_read" and config is not None:
            from .broker_board import read as read_board

            handler = lambda db, value, **kw: read_board(db, config, value, contract_reader=contract_reader, **kw)
        if request["method"] == "remote_read" and config is not None:
            from .broker_remote import read as read_remote

            handler = lambda db, value, **kw: read_remote(db, config, value, contract_reader=contract_reader, **kw)
        if request["method"] == "verification_list" and config is not None:
            from .project_verification_runs import read_for_worker

            handler = lambda db, value, **kw: read_for_worker(db, config, value, contract_reader=contract_reader, **kw)
        if handler is None:
            # Do not pretend unfinished claim/result/stop operations succeeded.
            error_code = "unavailable"
        else:
            try:
                result = handler(conn, request, peer_uid=peer.uid, now=now)
            except BudgetError:
                error_code = "unavailable"
                if request["method"] == "start":
                    from .broker_budget import block_unstarted

                    try:
                        block_unstarted(conn, params=request["params"], peer_uid=peer.uid, now=now)
                    except (ValueError, sqlite3.Error):
                        pass  # A concurrent takeover or DB failure grants no authority.
            except ValueError:
                error_code = "stale_binding"
            except sqlite3.Error:
                error_code = "unavailable"
        send_response(sock, request_id=request["request_id"], result=result,
                      error_code=error_code, timeout=timeout)
