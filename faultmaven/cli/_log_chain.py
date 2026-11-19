"""An ``fm-*`` command's first statement: its log lines and its process's own
outputs (an uncaught exception, a warning) through the API's chain on stderr,
since stdout is the operator's (principles §7; #2888)."""

import sys

from faultmaven.infrastructure.logging.config import install_log_handler


def install_command_logging() -> None:
    """The chain on stderr at the root's level, and the process outputs."""
    install_log_handler(sys.stderr, None)
