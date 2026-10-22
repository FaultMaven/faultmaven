"""Shared ``Case`` builders for unit tests (#2171).

Plain keyword-only functions with no hidden state: every call builds fresh
objects. A test file's own ``_case()`` helper calls one of these with the
values that make its case its own.

- :func:`make_case` builds a ``Case`` from the fields given.
- :func:`inquiry_case` is a ``make_case`` in INQUIRY.
- :func:`investigating_case` confirms the problem statement and builds the case
  in INVESTIGATING.

Any keyword a signature does not name is a ``Case`` field, passed to the
constructor as given. :data:`UNSET`, given for any keyword, leaves that field
to its model's default: it is not passed, so it stays outside
``model_fields_set``, exactly as when a helper never named it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from faultmaven.modules.case.contracts import (
    CaseState,
    InquiryData,
    ProblemStatus,
    ProblemVerification,
)
from faultmaven.modules.case.domain.models.case import Case


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


#: Leaves a field to its model's default (see the module docstring).
UNSET: Any = _Unset()


def _given(**fields: Any) -> dict[str, Any]:
    return {name: value for name, value in fields.items() if value is not UNSET}


def make_case(
    *,
    case_id: Optional[str] = None,
    user_id: Optional[str] = "u",
    enterprise_id: str = "o",
    title: str = "t",
    **fields: Any,
) -> Case:
    """A ``Case`` with ``fields``. ``case_id=None`` (the default) is a fresh
    ``case_<12 hex>`` per call."""
    if case_id is None:
        case_id = f"case_{uuid4().hex[:12]}"
    return Case(
        **_given(
            case_id=case_id,
            user_id=user_id,
            enterprise_id=enterprise_id,
            title=title,
            **fields,
        )
    )


def inquiry_case(*, statement: Optional[str] = None, **fields: Any) -> Case:
    """A case in INQUIRY. ``statement`` is proposed (not confirmed) on the
    case's inquiry after construction."""
    case = make_case(state=CaseState.INQUIRY, **fields)
    if statement is not None:
        case.inquiry.proposed_problem_statement = statement
    return case


def _confirm(inquiry: InquiryData, statement: Optional[str], confirmed_at: Any) -> None:
    if statement is not None:
        inquiry.proposed_problem_statement = statement
    inquiry.problem_statement_confirmed = True
    if confirmed_at is not UNSET:
        inquiry.problem_statement_confirmed_at = confirmed_at or datetime.now(
            timezone.utc
        )


def investigating_case(
    *,
    statement: Optional[str] = None,
    confirmed_at: Optional[datetime] = None,
    symptom: Optional[str] = None,
    severity: Optional[str] = "HIGH",
    problem_status: Optional[ProblemStatus] = None,
    description: str = "d",
    state: CaseState = CaseState.INVESTIGATING,
    inquiry: Optional[InquiryData] = None,
    **fields: Any,
) -> Case:
    """A case whose problem statement is confirmed, in ``state``.

    The statement is confirmed on ``inquiry`` (a fresh ``InquiryData`` when
    None) before the case is built in ``state``: ``statement`` becomes the
    proposed statement, and ``confirmed_at`` the confirmation time, now when
    None. ``inquiry=UNSET`` builds the case without an inquiry instead,
    confirms the statement on the case's own one, then moves the case to
    ``state``. Only there does ``confirmed_at=UNSET`` leave the time unset:
    building a ``Case`` around a given inquiry runs ``InquiryData``'s
    validator, which stamps a confirmed statement's time.

    ``symptom`` builds the ``problem_verification`` with ``severity``.
    ``problem_status`` is set on the case's progress after construction.
    """
    if symptom is not None:
        fields["problem_verification"] = ProblemVerification(
            **_given(symptom_statement=symptom, severity=severity)
        )
    if inquiry is UNSET:
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
