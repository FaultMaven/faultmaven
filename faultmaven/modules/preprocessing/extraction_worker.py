"""The Tier 1 worker: one extraction in its own process (#2637), a phased job
of ``worker_process`` run as ``python -m``. It imports the extractor class the
request names (none takes arguments), runs it over the content, and answers
``to_json()`` and the ``file_extract``, raw. It imports no service or settings.
"""

import importlib
import sys
from typing import Any, Callable, Dict, List

from faultmaven.infrastructure import worker_process
from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult


def _extract(request: Dict[str, Any], send) -> Callable[[], ExtractResult]:
    """Import the extractor the request names; the work runs it."""
    module_name, _, class_name = request["extractor"].partition(":")
    extractor = getattr(importlib.import_module(module_name), class_name)()
    (content,) = request["parts"]

    def work() -> ExtractResult:
        result = extractor.extract(content)
        # Every Tier 1 extractor returns ``ExtractResult`` (``protocol.py``):
        # a broken one fails here, by name, not as a result it cannot encode.
        if not isinstance(result, ExtractResult):
            raise TypeError(
                f"Extractor {class_name} returned {type(result).__name__}, "
                "expected ExtractResult"
            )
        return result

    return work


def _parts(result: ExtractResult) -> List[str]:
    return [result.to_json(), result.file_extract]


if __name__ == "__main__":
    sys.exit(worker_process.serve(_extract, "extraction", result_parts=_parts))
