"""The API's ASGI entry: the factory ``run_server`` gives uvicorn,
``faultmaven.asgi:create_app`` with ``factory=True``, in every process that
serves (#2888).

A uvicorn worker child (several workers, or the reloader's) imports nothing of
ours before it loads the app, and an exception raised there is printed by
``multiprocessing`` with its message, past every hook. :func:`create_app` puts
the process's outputs through the log chain first, then imports
``faultmaven.main``, which executes once, here. A failure is logged as its
class and frames and ends the process with ``SystemExit(1)``, which
``multiprocessing`` exits on without printing.
"""

import logging
from typing import TYPE_CHECKING

from faultmaven.infrastructure.logging.process_outputs import (
    install_process_outputs,
    sweep_own_handlers,
)

if TYPE_CHECKING:
    from fastapi import FastAPI


def create_app() -> "FastAPI":
    """The app ``faultmaven.main`` builds, once the process's outputs go
    through the chain."""
    install_process_outputs()
    sweep_own_handlers()
    try:
        from faultmaven.main import app
    except BaseException:  # noqa: BLE001 - every failure, rendered by the chain
        logging.getLogger(__name__).critical("The API failed to import", exc_info=True)
        raise SystemExit(1)
    return app
