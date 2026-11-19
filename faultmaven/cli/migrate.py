"""``fm-migrate``: upgrade the database to the newest migration, through the log
chain (#2888, #2898).

The ``alembic`` CLI catches a ``CommandError`` and prints ``FAILED: <its
text>`` outside logging, and logs the same text. This command calls
``alembic.command.upgrade`` itself, so a failure is an uncaught exception that
the process hooks render as its class and frames, and the exit status is 1. It
runs ``upgrade head`` only; the startup migration spawns it.
"""

# First: the process outputs go through the log chain from here on (#2888).
from faultmaven.cli import _log_chain  # isort: skip

import argparse

from faultmaven.cli._database_gate import require_persistent_configured_url_or_exit


def main() -> None:
    """Console entrypoint (``fm-migrate``)."""
    _log_chain.install_command_logging()
    from faultmaven.config.tls_root_set import apply_root_set

    apply_root_set()
    parser = argparse.ArgumentParser(
        prog="fm-migrate",
        description="Upgrade the database to head (alembic upgrade head).",
    )
    parser.add_argument(
        "--config",
        default="alembic.ini",
        help="The alembic.ini to read (default: the working directory's).",
    )
    args = parser.parse_args()
    require_persistent_configured_url_or_exit()
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(args.config), "head")


if __name__ == "__main__":
    main()
