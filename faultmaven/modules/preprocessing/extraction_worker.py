"""The Tier 1 worker: one extractor over one piece of evidence in its own
process (#2637).

Run as ``python -m`` this module by ``preprocessing_service``, one process per
extraction, which kills it at the Tier 1 deadline (Principle 14), through
``faultmaven.infrastructure.worker_process``: that module reads the request,
bounds the process and writes the frames. This one imports the extractor class
the request names (each takes no arguments), runs ``extract`` over the
content, and answers with ``ExtractResult.to_json()``. It imports the
extractors and nothing of the service, its settings or its models.
"""

import importlib
import sys
from typing import Any, Callable, Dict

from faultmaven.infrastructure import worker_process
from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult


def _extract(request: Dict[str, Any], send) -> Callable[[], str]:
    """Import the extractor the request names; the work runs it."""
    module_name, _, class_name = request["extractor"].partition(":")
    extractor = getattr(importlib.import_module(module_name), class_name)()
    content = request["content"]

    def work() -> str:
        result = extractor.extract(content)
        # Every Tier 1 extractor returns ``ExtractResult`` (``protocol.py``):
        # a broken one fails here, by name, not as a frame it cannot encode.
        if not isinstance(result, ExtractResult):
            raise TypeError(
                f"Extractor {class_name} returned {type(result).__name__}, "
                "expected ExtractResult"
            )
        return result.to_json()

    return work


if __name__ == "__main__":
    sys.exit(worker_process.serve(_extract, "extraction"))
