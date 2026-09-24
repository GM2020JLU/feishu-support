"""Control-side approved board queue consumer; no implicit lease or deployment."""

import sys

from .broker_board_cleanup import run_one as run_cleanup
from .broker_board_runner import run_one as run_action
from .broker_remote_service import main as consumer_main
from .executors import ExecutorError


def run_one(conn, config, **kwargs):
    result = run_cleanup(conn, config, **kwargs)
    if result["state"] not in ("idle", "cleanup_not_configured"):
        return result
    return run_action(conn, config, **kwargs)


def main(argv=None):
    try:
        return consumer_main(argv, consumer=run_one, description=__doc__)
    except ExecutorError:
        print("Board execution unavailable; inspect the recorded operation. Do not retry unknown device actions.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
