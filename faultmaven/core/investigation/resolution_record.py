"""What a resolve offer cites: the rows that earn it (#1906 D5.1).

Every resolve offer and close→resolve pivot says what the record shows and
nothing stronger (design P3). The offers used to assert a cause "confirmed
eliminated" and a problem that "went with it", composed from a verdict and
citing no row, which the engine said in the same reply that asked the user to
check whether the problem had gone.

Its own module, read only from ``cause_assurance``: the closure pivot in
``terminal_transitions`` and the replies in ``milestone_engine`` both compose
with it, and the replies' imports may not enter the transitions' graph (the
Case module layer contract in ``.importlinter``).
"""

from faultmaven.core.investigation.cause_assurance import (
    resolution_confirmation_rows,
    subject_index,
)
from faultmaven.modules.case.contracts import EvidenceCategory

#: How much of a row's summary (or a cause's statement) a record line quotes.
RECORD_TEXT_CHARS = 120


def _clip(text: str | None) -> str:
    text = " ".join((text or "").split()).rstrip(" .")
    if len(text) <= RECORD_TEXT_CHARS:
        return text
    return text[: RECORD_TEXT_CHARS - 1].rstrip() + "…"


def _row_text(row) -> str:
    return _clip(getattr(row, "summary", None) or getattr(row, "extract", None))


def resolution_record_lines(case, *, cause_on_record: bool = True) -> str:
    """The rows a resolve offer cites, as markdown bullets; ``""`` when the case
    confirms no resolution: for each cause the cause leg re-observed, its
    latest row; for each row that showed the problem and was re-checked, its
    latest re-check; then each row that showed the problem with no re-check in
    the problem leg. Read only from the confirmation's legs
    (``resolution_confirmation_rows``), the rows' data turns and their subjects
    (``subject_index``), and the verifying rows.

    ``cause_on_record=False`` (the backstop with no cause-identification leg)
    labels the cause line "no root cause on record" instead of naming the root
    the row re-checked. Residual, stated: "Not re-checked since the fix" names
    only RECORDED verifying rows; a signal the user reported but never
    submitted as data (#1906's Prometheus target) has no row to name.
    """
    rows = resolution_confirmation_rows(case)
    if not rows:
        return ""
    subjects = subject_index(case)
    nodes = getattr(case, "causal_nodes", None) or {}
    hypotheses = getattr(case, "hypotheses", None) or {}

    def latest(by: dict, key, row) -> None:
        if key not in by or subjects.observed(row) >= subjects.observed(by[key]):
            by[key] = row

    causes: dict = {}
    rechecked: dict = {}
    for row in rows:
        named = getattr(row, "rechecks_evidence_id", None)
        if row.category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE:
            latest(causes, subjects.cause_targets(named), row)
        else:
            latest(rechecked, named, row)

    lines = []
    for targets, row in causes.items():
        if not cause_on_record:
            label = "no root cause on record"
        else:
            # A rootless pool binds to its hypothesis (``cause_targets``).
            label = " / ".join(
                _clip(
                    nodes[t].statement
                    if t in nodes
                    else getattr(hypotheses.get(t), "statement", None)
                )
                for t in sorted(targets)
            )
        lines.append(
            f"- **The cause** — {label}: {_row_text(row)} "
            f"(turn {subjects.observed(row)})"
        )
    for verifying_id in subjects.verifying:
        if verifying_id in rechecked:
            row = rechecked[verifying_id]
            lines.append(
                f"- **The problem** — {_row_text(subjects.rows[verifying_id])}, "
                f"re-checked: {_row_text(row)} (turn {subjects.observed(row)})"
            )
    for verifying_id in subjects.verifying:
        if verifying_id not in rechecked and verifying_id in subjects.rows:
            lines.append(
                "- **Not re-checked since the fix** — "
                + _row_text(subjects.rows[verifying_id])
            )
    return "\n".join(lines)
