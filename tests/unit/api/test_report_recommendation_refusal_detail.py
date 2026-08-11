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

from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.case.api.routes import _recommendation_unavailable_detail


def _unreadable() -> KnowledgeBaseError:
    return KnowledgeBaseError(
        "matched rows could not be read",
        error_code="RUNBOOK_RESULTS_UNREADABLE",
    )


def test_the_unreadable_refusal_does_not_tell_the_operator_to_retry():
    """Retrying cannot clear it, so the text must not suggest waiting."""
    detail = _recommendation_unavailable_detail(_unreadable())

    assert "Retrying will not clear this." in detail
    assert "Retry once the knowledge base is available" not in detail
    assert "re-indexing" in detail


def test_the_unreadable_refusal_does_not_claim_the_knowledge_base_is_down():
    """The knowledge base answered; it is the rows that are unreadable."""
    detail = _recommendation_unavailable_detail(_unreadable())

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
    detail = _recommendation_unavailable_detail(_unreadable())

    assert "only runbooks it could not read" not in detail
    assert "closest-matching" in detail


@pytest.mark.parametrize(
    "exc",
    [
        KnowledgeBaseError("boom", error_code="RUNBOOK_SEARCH_FAILED"),
        KnowledgeBaseError("no tenant", error_code="RUNBOOK_SEARCH_UNSCOPED"),
        TimeoutError("timed out"),
        RuntimeError("circuit open"),
    ],
)
def test_every_transient_cause_keeps_the_retry_advice(exc):
    """The other causes ARE transient, and must keep the advice that fits them.

    Covers the exception types this handler catches, including the two that
    arrive unwrapped from ``call_external`` and carry no ``error_code`` at all
    — the ``getattr`` default has to route those here, not to the deterministic
    message.
    """
    detail = _recommendation_unavailable_detail(exc)

    assert "Retry once the knowledge base is available" in detail
    assert "re-indexing" not in detail
