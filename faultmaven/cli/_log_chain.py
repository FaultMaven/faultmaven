"""An ``fm-*`` command's log chain (principles §7; #2888).

Every command module imports this module first, ahead of its own imports, so
the process's own outputs (an uncaught exception, a warning) go through the
chain from there on. ``main()``'s first statement, :func:`install_command_logging`,
then gives the root the chain's handler on stderr, since stdout is the
operator's."""

import sys

from faultmaven.infrastructure.logging.process_outputs import install_process_outputs

install_process_outputs()

from faultmaven.infrastructure.logging.config import install_log_handler  # noqa: E402


def install_command_logging() -> None:
    """The chain on stderr at the root's level, and the process outputs."""
    install_log_handler(sys.stderr, None)
