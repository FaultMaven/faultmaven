"""The preparation worker: one document's Stages 0-5 in its own process (#2622).

Run as ``python -m`` this module by ``document_preprocessor``, one process per
job, which kills it at the deadline (Principle 14), through
``faultmaven.infrastructure.worker_process``: that module reads the request,
bounds the process and writes the frames. This one names the stage, applies
pypdf's limits, and sends ``{"stage": n}`` as each stage starts. The parent
emits the log records under its own request context, so the worker imports no
settings.
"""

import importlib
import sys
from pathlib import Path
from typing import Any, Callable, Dict

from faultmaven.infrastructure import worker_process


def _prepare(request: Dict[str, Any], send) -> Callable[[], Any]:
    """Import the stage the request names; the work runs it over the file."""
    module_name, _, function_name = request["stage"].partition(":")
    stage = getattr(importlib.import_module(module_name), function_name)
    from pypdf import Configuration, apply_configuration

    def work() -> Any:
        with apply_configuration(Configuration(**request["pdf_limits"])):
            return stage(
                Path(request["file_path"]),
                request["content_type"],
                lambda n: send({"stage": n}),
            )

    return work


if __name__ == "__main__":
    sys.exit(worker_process.serve(_prepare, "preparation"))
