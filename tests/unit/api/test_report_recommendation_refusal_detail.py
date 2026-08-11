"""The 503 an unreadable-runbook refusal produces (#912).

A refusal is only useful if it points at the right remedy. Every other error
reaching this handler is transient and clears on retry;
``RUNBOOK_RESULTS_UNREADABLE`` never does — the knowledge base is up, and the
offending rows fail identically until they are re-indexed.

These tests exist because the branch that distinguishes them shipped with no
coverage at all: inverting its condition, so every 503 carried the wrong
remediation, passed the entire API suite.
"""

import pytest

from faultmaven.infrastructure.base_client import CircuitBreakerError
from faultmaven.infrastructure.knowledge.runbook_kb import RESULTS_UNREADABLE_CODE
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.case.api.routes import _recommendation_unavailable_detail


def _unreadable() -> KnowledgeBaseError:
    return KnowledgeBaseError(
        "matched rows could not be read",
        error_code="RUNBOOK_RESULTS_UNREADABLE",
    )


def test_the_unreadable_refusal_does_not_tell_the_operator_to_retry():
    """Retrying cannot clear it, so the text must not suggest waiting."""
    detail = _recommendation_unavailable_detail(True)

    assert "Retrying will not clear this." in detail
    assert "Retry once the knowledge base is available" not in detail
    assert "re-indexing" in detail


def test_the_unreadable_refusal_does_not_claim_the_knowledge_base_is_down():
    """The knowledge base answered; it is the rows that are unreadable."""
    detail = _recommendation_unavailable_detail(True)

    assert "knowledge base is available" in detail
    assert "search is unavailable" not in detail


def test_the_unreadable_refusal_does_not_claim_every_row_was_unreadable():
    """The refusal fires on MIXED sets, so "only" would be a false statement.

    It triggers whenever an unreadable runbook outranks every readable one —
    which includes result sets where other runbooks were read perfectly well.
    An earlier wording said "found only runbooks it could not read"; that was
    true of the first version of the rule and false of the shipped one, and no
    test noticed.
    """
    detail = _recommendation_unavailable_detail(True)

    assert "only runbooks it could not read" not in detail
    assert "closest-matching" in detail


@pytest.mark.parametrize(
    "exc",
    [
        KnowledgeBaseError("boom", error_code="RUNBOOK_SEARCH_FAILED"),
        KnowledgeBaseError("no tenant", error_code="RUNBOOK_SEARCH_UNSCOPED"),
        TimeoutError("timed out"),
        # The REAL CircuitBreakerError, not a stand-in: it defines its own
        # ``error_code`` (it forwards the code of whatever tripped the breaker),
        # so a `RuntimeError` substitute would exercise the ``getattr`` default
        # on a type that never reaches it and prove nothing about this one.
        CircuitBreakerError("circuit open for RunbookKB"),
        CircuitBreakerError("circuit open", error_code="RUNBOOK_SEARCH_FAILED"),
    ],
)
def test_no_transient_cause_classifies_as_unreadable(exc):
    """None of the transient causes carry the deterministic code.

    The route classifies with ``getattr(e, "error_code", None) ==
    RESULTS_UNREADABLE_CODE`` and passes the verdict, so this pins the half that
    depends on the exceptions themselves. It matters most for
    ``CircuitBreakerError``, which really does define ``error_code`` (it
    forwards the code of whatever tripped the breaker) — an open breaker is
    transient by construction, so it must never inherit "retrying will not
    clear this".
    """
    assert getattr(exc, "error_code", None) != RESULTS_UNREADABLE_CODE

    detail = _recommendation_unavailable_detail(False)
    assert "Retry once the knowledge base is available" in detail
    assert "re-indexing" not in detail


def test_the_code_the_route_branches_on_is_the_one_the_knowledge_base_raises():
    """Both sides name the same constant, so the branch cannot rot silently.

    A literal on each side would let a typo in one file restore the misleading
    generic message with every test still green — the branch would simply never
    match.
    """
    kb_error = _unreadable()

    assert kb_error.error_code == RESULTS_UNREADABLE_CODE
    assert _recommendation_unavailable_detail(
        kb_error.error_code == RESULTS_UNREADABLE_CODE
    ) != _recommendation_unavailable_detail(False)
