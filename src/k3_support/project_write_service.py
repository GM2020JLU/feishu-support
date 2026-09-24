"""Dedicated private field/transition sender. No migrations, login or deployment changes."""

from .project_refresh_service import main as private_service_main
from .project_write_dispatch import run_one


def main(argv=None):
    return private_service_main(argv, consumer=run_one)


if __name__ == "__main__":
    raise SystemExit(main())
