"""The runbook gate's worker (#2661): one check over one body, a phased job of
``worker_process`` run as ``python -m`` by ``runbook_gate``. A refusal is an
answer, never a failure. It imports nothing of ``modules.case``.
"""

import json
import sys
from typing import Any, Callable, Dict, List

from faultmaven.infrastructure import worker_process
from faultmaven.modules.knowledge.domain.services import runbook_validator as rv


def _scored(content: str) -> Dict[str, Any]:
    validation, quality = rv.validate_and_score(content)
    return {"validation": validation.model_dump(), "quality": quality.model_dump()}


def _enforcing(check: Callable[[str], None]) -> Callable[[str], Dict[str, Any]]:
    def answer(content: str) -> Dict[str, Any]:
        try:
            check(content)
        except rv.RunbookQualityError as refused:
            return {"refused": {"errors": refused.errors, "warnings": refused.warnings}}
        return {"refused": None}

    return answer


#: The checks, by the name the request gives.
CHECKS: Dict[str, Callable[[str], Dict[str, Any]]] = {
    "validate_and_score": _scored,
    "enforce_runbook_quality": _enforcing(rv.enforce_runbook_quality),
    "enforce_runbook_security": _enforcing(rv.enforce_runbook_security),
}


def _check(request: Dict[str, Any], send) -> Callable[[], Dict[str, Any]]:
    check = CHECKS[request["check"]]
    (content,) = request["parts"]
    return lambda: check(content)


def _parts(answer: Dict[str, Any]) -> List[str]:
    return [json.dumps(answer)]


if __name__ == "__main__":
    sys.exit(worker_process.serve(_check, "runbook check", result_parts=_parts))
