from __future__ import annotations


class TransitionError(ValueError):
    pass


ACTIVE_STATES = {
    "intake",
    "triage",
    "answering",
    "investigating",
    "waiting_board",
    "board_testing",
    "waiting_push",
    "monitoring",
    "paused",
    "escalated",
    "error",
}
TERMINAL_STATES = {"resolved", "takeover", "cancelled"}
ALL_STATES = ACTIVE_STATES | TERMINAL_STATES

TRANSITIONS: dict[str, set[str]] = {
    "intake": {"triage", "paused", "takeover", "escalated", "cancelled", "error"},
    "triage": {
        "answering",
        "investigating",
        "paused",
        "takeover",
        "escalated",
        "cancelled",
        "error",
    },
    "answering": {
        "resolved",
        "investigating",
        "paused",
        "takeover",
        "escalated",
        "cancelled",
        "error",
    },
    "investigating": {
        "answering",
        "waiting_board",
        "waiting_push",
        "monitoring",
        "resolved",
        "paused",
        "takeover",
        "escalated",
        "cancelled",
        "error",
    },
    "waiting_board": {
        "answering",
        "board_testing",
        "investigating",
        "paused",
        "takeover",
        "escalated",
        "cancelled",
        "error",
    },
    "board_testing": {
        "answering",
        "investigating",
        "waiting_push",
        "monitoring",
        "resolved",
        "paused",
        "takeover",
        "escalated",
        "cancelled",
        "error",
    },
    "waiting_push": {
        "answering",
        "monitoring",
        "investigating",
        "resolved",
        "paused",
        "takeover",
        "escalated",
        "cancelled",
        "error",
    },
    "monitoring": {
        "answering",
        "resolved",
        "investigating",
        "paused",
        "takeover",
        "escalated",
        "cancelled",
        "error",
    },
    "paused": {
        "triage",
        "answering",
        "investigating",
        "waiting_board",
        "waiting_push",
        "monitoring",
        "takeover",
        "cancelled",
        "error",
    },
    "escalated": {
        "answering",
        "investigating",
        "paused",
        "takeover",
        "resolved",
        "cancelled",
        "error",
    },
    "error": {"triage", "investigating", "paused", "takeover", "cancelled"},
    "resolved": set(),
    "takeover": set(),
    "cancelled": set(),
}


def require_transition(before: str, after: str) -> None:
    if before not in ALL_STATES or after not in ALL_STATES:
        raise TransitionError(f"unknown state: {before!r} -> {after!r}")
    if after not in TRANSITIONS[before]:
        raise TransitionError(f"illegal transition: {before} -> {after}")
