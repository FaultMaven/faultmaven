"""The ``<evidence_needs>`` block's call site (#2811 S2b-1b-ii).

The block is rendered by ``narration.evidence_needs``. Its need sections read
the prompt assembly's needs pool (``NarrationFold.needs``) and ``content(id)``;
a failure there propagates, as the block's did on ``main``. Its
re-verification checklist, asked only on an INVESTIGATING case in MITIGATION
or TREATMENT, reads the assembly's record, ``content(id)`` and, where no row
verified the problem, every symptom row (``EvidenceNeedsFresh``, read here).
If any of that raises, or the record does not fold, the checklist is the one
declared line ``CHECKLIST_OMITTED``, counted once per assembly with the
``<fix_verification>`` block's failure (``NarrationFold.fail``).
"""

from typing import Optional

from faultmaven.core.investigation.narration.evidence_needs import (
    EvidenceNeedsFresh,
    recheck_checklist,
    render_evidence_needs,
)
from faultmaven.core.investigation.narration.fix_verification import (
    CaseIdentity,
    content_by_id,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    EvidenceCategory,
    InvestigationStage,
)

from .narration_fold import NarrationFold

_BLOCK = "<evidence_needs> re-verification checklist"


def _uncited_symptom_rows(case: Case) -> EvidenceNeedsFresh:
    """Every symptom_evidence row, stable by its established turn."""
    rows = sorted(
        (
            ev
            for ev in case.evidence
            if ev.category == EvidenceCategory.SYMPTOM_EVIDENCE
        ),
        key=lambda ev: ev.collected_at_turn,
    )
    return EvidenceNeedsFresh(tuple(ev.evidence_id for ev in rows))


def _build_evidence_needs_block(
    case: Case, fold: Optional[NarrationFold] = None
) -> str:
    """The block, or ``""`` (``render_evidence_needs``). ``fold`` is the prompt
    assembly's (a fresh one per build without it)."""
    identity = CaseIdentity.of(case)
    fold = NarrationFold(case) if fold is None else fold
    content = lambda item_id: content_by_id(case, item_id)  # noqa: E731
    checklist: Optional[tuple[str, ...]] = ()
    if identity.state == CaseState.INVESTIGATING and identity.current_stage in (
        InvestigationStage.MITIGATION,
        InvestigationStage.TREATMENT,
    ):
        try:
            outcome = fold.outcome(_BLOCK)
            checklist = None
            if outcome is not None:
                fresh = (
                    None
                    if outcome.problem.verifying_row_ids
                    else _uncited_symptom_rows(case)
                )
                checklist = recheck_checklist(outcome, content, fresh)
        except Exception:  # noqa: BLE001 — only the checklist is lost (#2811)
            fold.fail(_BLOCK)
            checklist = None
    return render_evidence_needs(identity, content, fold.needs, checklist)
