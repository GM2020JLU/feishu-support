"""Optional operator-configured ledger gate for worker semantic calls.

Not a provider billing guarantee: manifest identity is local configuration,
and absent authenticated usage receipts retain the full attempt reservation.
"""

import os
import sqlite3

from .budget_blocks import record, resolve
from .hermes_stdin import BridgeError, manifest
from .ids import digest
from .model_budget import BudgetError, invoke
from .semantic import (
    budget_transport,
    expected_bridge_identity,
    expected_bridge_manifest,
)


def selector(conn, config, function, *, scope, case_id=None):
    """Scope is a worker event/job identity, never sourced from model text."""

    def call(*args, **kwargs):
        try:
            policy = conn.execute(
                "SELECT * FROM model_budget_policy WHERE singleton=1"
            ).fetchone()
            if policy is None and not os.environ.get("K3_SUPPORT_HERMES_BRIDGE_CONFIG"):
                return function(
                    *args, executable=config.runtime("semantic_command"), **kwargs
                )
            identity = manifest(os.environ.get("K3_SUPPORT_HERMES_BRIDGE_CONFIG"))
        except sqlite3.Error:
            record(conn, scope, function.__name__, "ledger_unavailable")
            return None
        except (OSError, ValueError, BridgeError):
            record(conn, scope, function.__name__, "identity_unverified")
            return None

        def gate(request, transport):
            try:
                if policy is None:
                    if manifest(os.environ.get("K3_SUPPORT_HERMES_BRIDGE_CONFIG")) != identity:
                        record(conn, scope, function.__name__, "identity_unverified")
                        return None
                    return transport()
                value = invoke(
                    conn,
                    transport=transport,
                    before_dispatch=lambda: manifest(os.environ.get("K3_SUPPORT_HERMES_BRIDGE_CONFIG")) == identity,
                    request_id="semantic:"
                    + digest(
                        {
                            "scope": scope,
                            "function": function.__name__,
                            "request": request,
                        }
                    ),
                    case_id=case_id,
                    provider=identity["provider"],
                    model=identity["model"],
                    amount=policy["attempt_limit"],
                    input_digest=digest(request),
                )["value"]
                if value is not None:
                    resolve(conn, scope, function.__name__)
                else:
                    record(conn, scope, function.__name__, "model_result_unconfirmed")
                return value
            except BudgetError:
                record(conn, scope, function.__name__, "budget_gate_blocked")
                return None
            except (OSError, ValueError):
                record(conn, scope, function.__name__, "identity_unverified")
                return None
            except sqlite3.Error:
                record(conn, scope, function.__name__, "ledger_unavailable")
                return None

        token = budget_transport.set(gate)
        identity_token = expected_bridge_manifest.set(digest(identity))
        descriptor_token = expected_bridge_identity.set(identity)
        try:
            return function(
                *args, executable=config.runtime("semantic_command"), **kwargs
            )
        finally:
            expected_bridge_identity.reset(descriptor_token)
            expected_bridge_manifest.reset(identity_token)
            budget_transport.reset(token)

    return call
