"""One prompt assembly's narration inputs (#2811 S2b-1a, S2b-1b-ii): the
record ``narration_outcome`` folds and the needs pool's view, each computed
at most once and shared by the ``<fix_verification>`` and ``<evidence_needs>``
blocks."""

import logging
import sys
import traceback
from typing import Optional

from faultmaven.core.investigation.lifecycle_metrics import (
    case_outcome_write_failures_total,
)
from faultmaven.core.investigation.narration.needs import NeedsView
from faultmaven.core.investigation.outcome import narration_outcome
from faultmaven.modules.case.contracts import Case, CaseOutcome, WritePoint

logger = logging.getLogger(__name__)


class NarrationFold:
    """One prompt assembly's narration fold: the record, computed at most
    once, on the first request, and cached with its failure; and the needs
    pool's view (``needs``), computed once and cached, a raise not cached.
    ``get_prompt_for_case`` makes one per assembly, so the context it builds
    up to three times folds once and reads the pool once; each block that
    renders an omission logs each distinct failure once, naming itself, and a
    failure is counted once per assembly (``fail``)."""

    def __init__(self, case: Case) -> None:
        self._case = case
        self._outcome: Optional[CaseOutcome] = None
        self._error: Optional[BaseException] = None
        self._needs: Optional[NeedsView] = None
        self._asked = False
        self._failed = False
        self._logged: set = set()

    def outcome(self, block: str) -> Optional[CaseOutcome]:
        """The record, or None when the fold raised: then ``block``, which
        renders its omission, logs the fold's failure (``fail``)."""
        if not self._asked:
            self._asked = True
            try:
                self._outcome = narration_outcome(self._case)
            except Exception as error:  # noqa: BLE001 — only what reads it is lost
                self._error = error
        if self._error is not None:
            self.fail(block, self._error)
        return self._outcome

    def needs(self) -> NeedsView:
        """The needs pool's view (ruling 2 of 2026-11-17). A raise propagates
        and is not cached: ``<evidence_needs>`` read the pool unguarded on
        ``main``."""
        if self._needs is None:
            self._needs = NeedsView.of(self._case)
        return self._needs

    def fail(self, block: str, error: Optional[BaseException] = None) -> None:
        """Log ``error`` (the exception being handled without it) once per
        block and distinct failure (its class and the line that raised it),
        naming ``block``, and count a failure once per assembly; a metrics
        error is logged and swallowed."""
        error = sys.exc_info()[1] if error is None else error
        tb = getattr(error, "__traceback__", None)
        raised = traceback.extract_tb(tb)[-1] if tb is not None else None
        key = (block, type(error), raised and (raised.filename, raised.lineno))
        if key not in self._logged:
            self._logged.add(key)
            logger.error(
                "Case %s: the %s was not rendered (#2811)",
                getattr(self._case, "case_id", None),
                block,
                exc_info=error,
            )
        if self._failed:
            return
        self._failed = True
        try:
            case_outcome_write_failures_total.labels(at=WritePoint.NARRATION).inc()
        except Exception:  # noqa: BLE001 — the block is omitted either way
            logger.exception("The narration failure was not counted (#2811)")
