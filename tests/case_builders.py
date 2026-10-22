"""Shared ``Case`` builders for unit tests (#2171).

Plain keyword-only functions with no hidden state: every call builds fresh
objects, and a sub-object a caller passes in is copied before a builder sets
anything on it. A test file's own ``_case()`` helper calls one of these with
the values that make its case its own.

- :func:`make_case` builds a ``Case`` from the fields given.
- :func:`inquiry_case` is a ``make_case`` in INQUIRY.
- :func:`investigating_case` confirms the problem statement and builds the case
  in INVESTIGATING.

Any keyword a signature does not name is a ``Case`` field, passed to the
constructor as given.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from faultmaven.modules.case.contracts import (
    CaseState,
    InquiryData,
    ProblemStatus,
    ProblemVerification,
)
from faultmaven.modules.case.domain.models.case import Case


def make_case(
    *,
    user_id: Optional[str] = "u",
    enterprise_id: str = "o",
    title: str = "t",
    **fields: Any,
) -> Case:
    """A ``Case`` with ``fields``; ``Case`` mints the ``case_id`` unless one
    is given."""
    return Case(user_id=user_id, enterprise_id=enterprise_id, title=title, **fields)


def _own_inquiry(fields: dict[str, Any]) -> Optional[InquiryData]:
    given = fields.pop("inquiry", None)
    return None if given is None else given.model_copy(deep=True)


def inquiry_case(*, statement: Optional[str] = None, **fields: Any) -> Case:
    """A case in INQUIRY. ``statement`` is proposed (not confirmed) on the
    case's inquiry after construction."""
    inquiry = _own_inquiry(fields)
    if inquiry is not None:
        fields["inquiry"] = inquiry
    case = make_case(state=CaseState.INQUIRY, **fields)
    if statement is not None:
        case.inquiry.proposed_problem_statement = statement
    return case


def _confirm(
    inquiry: InquiryData, statement: Optional[str], confirmed_at: Optional[datetime]
) -> None:
    if statement is not None:
        inquiry.proposed_problem_statement = statement
    inquiry.problem_statement_confirmed = True
    if confirmed_at is not None:
        inquiry.problem_statement_confirmed_at = confirmed_at
    elif inquiry.problem_statement_confirmed_at is None:
        inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)


def investigating_case(
    *,
    statement: Optional[str] = None,
    confirmed_at: Optional[datetime] = None,
    confirm_after_build: bool = False,
    symptom: Optional[str] = None,
    severity: Optional[str] = "HIGH",
    temporal_state: Optional[str] = None,
    urgency: Optional[str] = None,
    problem_status: Optional[ProblemStatus] = None,
    description: str = "d",
    state: CaseState = CaseState.INVESTIGATING,
    **fields: Any,
) -> Case:
    """A case whose problem statement is confirmed, in ``state``.

    ``statement`` becomes the proposed statement and ``confirmed_at`` the
    confirmation time (now, unless given or already set). The statement is
    confirmed on the inquiry before the case is built, so the confirmation
    precedes the case's own timestamps. With ``confirm_after_build`` the case
    is built in INQUIRY first, then confirmed and moved to ``state``, the way
    Gate 1 moves a live case.

    ``symptom`` builds the ``problem_verification``, with ``severity``,
    ``temporal_state`` and ``urgency`` (its ``urgency_level``).
    ``problem_status`` is set on the case's progress after construction.
    """
    if symptom is not None:
        given = {"temporal_state": temporal_state, "urgency_level": urgency}
        fields["problem_verification"] = ProblemVerification(
            symptom_statement=symptom,
            severity=severity,
            **{name: value for name, value in given.items() if value is not None},
        )
    inquiry = _own_inquiry(fields)
    if confirm_after_build:
        if inquiry is not None:
            fields["inquiry"] = inquiry
        case = make_case(description=description, **fields)
        _confirm(case.inquiry, statement, confirmed_at)
        case.state = state
    else:
        inquiry = InquiryData() if inquiry is None else inquiry
        _confirm(inquiry, statement, confirmed_at)
        case = make_case(
            description=description, state=state, inquiry=inquiry, **fields
        )
    if problem_status is not None:
        case.progress.problem_status = problem_status
    return case
