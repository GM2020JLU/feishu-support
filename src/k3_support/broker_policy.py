"""Shared runtime gate; budgeted sessions require control-loaded identity."""

from .broker_execution_contract import ExecutionContract
from .runtime_control import capability_allowed


def execution_allowed(conn, config, *, contract=None):
    # Only the control-side loader supplies this value, never worker RPC fields.
    # Start reserves atomically; claims alone do not authorize model execution.
    if conn.execute("SELECT 1 FROM model_budget_policy WHERE singleton=1").fetchone() and type(contract) is not ExecutionContract:
        return False
    return config.feature("codex") and capability_allowed(conn, config, "codex")
