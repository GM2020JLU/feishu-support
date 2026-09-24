"""Shared worker guidance; control-side checks remain authoritative."""

GUIDANCE = (
    "\nBefore verification work, call k3_remote verification_list with a fresh query "
    "request_id and after_id=''; follow next_cursor. This read can discover requests "
    "prepared after your task started. Only submit dispatchable items. Copy their "
    "remote_request_id unchanged as remote_submit request_id, and copy the exact "
    "remote mode/repo/command. Do not substitute commands or generate a new execution "
    "ID for a prepared request. For queued/running/unknown or uncertain delivery, "
    "read the original remote request instead of resubmitting with another ID. "
    "The list confers no additional permission. Command success is execution "
    "evidence only; never claim verified source, environment or functionality "
    "from exit zero. If no suitable request exists, report that verification "
    "preparation is needed. For device steps, the prepared command itself drives "
    "the board and serial capture; run it unmodified and never fabricate or edit "
    "device output.\n"
)


def task_guidance(inputs):
    """Prepared verification requests are exclusive only for verification jobs."""
    if "verification" in inputs:
        return GUIDANCE
    return (
        "\nThis is an ordinary coding task, not a prepared verification step. "
        "Use scoped k3_remote work requests with fresh canonical UUIDs for baseline "
        "tests, candidate tests, and local commits. Run the test process as the "
        "final command so its actual exit status is retained; inspect logs in a "
        "separate read request. Do not interpret an empty verification_list as a "
        "ban on testing this coding task. Report command request IDs and distinguish "
        "execution success from repair and verification conclusions.\n"
    )
