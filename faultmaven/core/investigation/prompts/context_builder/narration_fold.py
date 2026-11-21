"""One prompt assembly's narration inputs (#2811 S2b-1a, S2b-1b-ii,
S2b-1c-i): the record ``narration_outcome`` folds, the needs pool's view, the
prompt state's fresh inputs (``PromptStateFresh``) and the two fresh reads
printed beside the record (the symptom's currency note, ``fix_confirmed``),
each computed at most once and shared by every block that reads them:
``<fix_verification>``, ``<evidence_needs>``, the milestone status, the
exclusion rule, the focus emphasis, the state summary, the causal graph's
recovery notes and the working conclusion's contest NOTE (S2b-2a), and the
fallback prompt the assembly may return instead."""

import logging
import sys
import traceback
from typing import Callable, Optional, TypeVar

from faultmaven.core.investigation.lifecycle_metrics import (
    case_outcome_write_failures_total,
)
from faultmaven.core.investigation.narration.needs import NeedsView
from faultmaven.core.investigation.narration.prompt_state import PromptStateFresh
from faultmaven.core.investigation.offers import offer_view
from faultmaven.core.investigation.outcome import narration_outcome
from faultmaven.modules.case.contracts import Case, CaseOutcome, WritePoint

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _stored_mitigation_verified(case: Case) -> bool:
    """``mitigation.verified`` as stored, read fresh (ruling Q1 (a))."""
    mitigation = case.progress.mitigation
    return bool(mitigation is not None and mitigation.verified)


def _symptom_stale(case: Case) -> bool:
    """The symptom's observation window sits away from the present. False
    for UNDATED: an unknown observation time gives nothing to anchor to."""
    from faultmaven.core.investigation.symptom_currency import (
        SymptomCurrency,
        assess_symptom_currency,
    )

    return assess_symptom_currency(case) == SymptomCurrency.STALE


def _currency_note(case: Case) -> str:
    """The currency note on ``- symptom_verified`` (``_symptom_currency_note``,
    declared fresh since S2b-1a: it reads the wall clock)."""
    from .evidence import _symptom_currency_note

    return _symptom_currency_note(case, "symptom_verified")


def _fix_confirmed(case: Case) -> bool:
    """The latest decided fix window SUCCEEDED (ruling Q1 (a) keeps it fresh
    until stage S3)."""
    from .causal_graph_block import fix_confirmed

    return fix_confirmed(case)


class NarrationFold:
    """One prompt assembly's narration fold: the record, computed at most
    once, on the first request, and cached with its failure; the needs
    pool's view (``needs``), computed once and cached, a raise not cached;
    and the fresh inputs (``fresh``, ``currency_note``, ``fix_confirmed``),
    each read on its first request and cached with its failure.
    ``get_prompt_for_case`` makes one per assembly and hands it to the
    fallback prompt it may return instead, so the context it builds up to
    three times, and every candidate fallback render, fold once and read the
    pool once; ``get_fallback_prompt_for_case`` called alone makes one per
    call. Each block that renders an omission logs each distinct failure
    once, naming itself, and a failure is counted once per assembly
    (``fail``)."""

    def __init__(self, case: Case) -> None:
        self._case = case
        self._outcome: Optional[CaseOutcome] = None
        self._error: Optional[BaseException] = None
        self._needs: Optional[NeedsView] = None
        self._fresh: Optional[PromptStateFresh] = None
        self._read: dict = {}
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
            self.fail(block, self._error, record=True)
        return self._outcome

    def needs(self) -> NeedsView:
        """The needs pool's view (ruling 2 of 2026-11-17). A raise propagates
        and is not cached: ``<evidence_needs>`` read the pool unguarded on
        ``main``."""
        if self._needs is None:
            self._needs = NeedsView.of(self._case)
        return self._needs

    def fresh(self) -> PromptStateFresh:
        """The prompt state's fresh inputs (#2811 S2b-1c-i), the offer facts
        among them (``offer_view``, S2b-2d: at most one view, each decline's
        turn read on its first ask). Each reads the case only when a selector asks
        for it, where ``main`` read it, and at most once per assembly; a
        raise is kept and raised to every asker, whose block renders its
        omission (``fail``)."""
        if self._fresh is None:
            self._fresh = PromptStateFresh(
                mitigation_verified=self._once(_stored_mitigation_verified),
                symptom_stale=self._once(_symptom_stale),
                offers=self._once(offer_view),
            )
        return self._fresh

    def currency_note(self) -> str:
        """The currency note, read where ``- symptom_verified`` prints. A
        raise propagates, as on ``main``."""
        return self._once(_currency_note)()

    def fix_confirmed(self) -> bool:
        """``fix_confirmed``, read where the exclusion reminder is decided. A
        raise propagates, as on ``main``."""
        return self._once(_fix_confirmed)()

    def _once(self, read: Callable[[Case], _T]) -> Callable[[], _T]:
        def ask() -> _T:
            if read not in self._read:
                try:
                    self._read[read] = (read(self._case), None)
                except Exception as error:  # noqa: BLE001 — its asker omits
                    self._read[read] = (None, error)
            value, error = self._read[read]
            if error is not None:
                raise error
            return value

        return ask

    def fail(
        self, block: str, error: Optional[BaseException] = None, *, record: bool = False
    ) -> None:
        """Log ``error`` (the exception being handled without it) once per
        block and distinct failure (its class and the line that raised it),
        naming ``block``, and count a failure once per assembly; a metrics
        error is logged and swallowed. A ``record`` failure (the fold raised)
        is logged as the record not read for ``block``, any other as the
        block not rendered. The record line claims nothing about display:
        the causal graph block and the working conclusion's contest NOTE
        (S2b-2a) are built whether or not the template shows them."""
        error = sys.exc_info()[1] if error is None else error
        tb = getattr(error, "__traceback__", None)
        raised = traceback.extract_tb(tb)[-1] if tb is not None else None
        key = (block, type(error), raised and (raised.filename, raised.lineno))
        if key not in self._logged:
            self._logged.add(key)
            logger.error(
                (
                    "Case %s: the record could not be read for the %s (#2811)"
                    if record
                    else "Case %s: the %s was not rendered (#2811)"
                ),
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
