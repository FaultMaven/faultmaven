"""The operator metadata/content boundary as a model-shape invariant (ADR-012 D9).

The cloud operator case list must never carry user free text. The route enforces
that by returning a model that has no such field to populate — so the guarantee
is really a property of ``AdminCaseMetadata``'s field set, and that is what these
tests pin.

They are written against the field sets rather than against a sample row on
purpose: an example proves one case is clean, while "every field of
``CaseSummary`` is classified" stays true as the case model grows. Adding a field
to ``CaseSummary`` fails these tests until it is put on one side of the boundary.
"""

from typing import get_args

import pytest

from faultmaven.models.api_models import (
    CASE_SUMMARY_CONTENT_FIELDS,
    AdminCaseMetadata,
    CaseSummary,
)

pytestmark = pytest.mark.unit


# Fields of ``CaseSummary`` that are NOT content but are still left out of the
# operator metadata row, each for a reason that is not "it leaks user text":
#
#   shared_team_ids  — ``list_all_cases`` does not run the team-share enrichment
#       (only the per-user list does), so this would be an unconditionally empty
#       list. Shipping a field that always reads "no teams" is worse than
#       omitting it: an operator would read the absence as fact.
#   valid_next_states — the transitions the *owner* may drive from the dashboard.
#       An operator does not act on tenant cases from this list, so it is an
#       affordance with no consumer here.
DELIBERATELY_OMITTED_FIELDS = frozenset({"shared_team_ids", "valid_next_states"})


def test_every_case_summary_field_is_classified():
    """No ``CaseSummary`` field may be unclassified.

    The guard that actually holds the boundary over time. A field added to
    ``CaseSummary`` lands in exactly one of three buckets — carried as metadata,
    declared content, or deliberately omitted — and this fails until the author
    picks one. Without it, a new free-text field could be copied into
    ``AdminCaseMetadata`` without anyone deciding it was safe to disclose.
    """
    classified = (
        set(AdminCaseMetadata.model_fields)
        | set(CASE_SUMMARY_CONTENT_FIELDS)
        | set(DELIBERATELY_OMITTED_FIELDS)
    )
    unclassified = set(CaseSummary.model_fields) - classified

    assert not unclassified, (
        f"CaseSummary fields {sorted(unclassified)} are neither carried as "
        "operator metadata, declared content, nor deliberately omitted. "
        "Classify each one (ADR-012 D9) before it reaches the cloud operator list."
    )


def test_metadata_carries_no_content_field():
    """The content fields are structurally absent, not blanked out."""
    leaked = set(AdminCaseMetadata.model_fields) & set(CASE_SUMMARY_CONTENT_FIELDS)
    assert not leaked, f"AdminCaseMetadata declares content field(s): {sorted(leaked)}"


def test_metadata_validation_is_no_stricter_than_case_summary():
    """Nothing accepted upstream may be rejected here, so the projection is total.

    ``AdminCaseMetadata`` re-declares its field types rather than deriving them,
    which leaves room for the two to drift — and the cloud arm builds the whole
    page in one comprehension, so a single row that validated as a
    ``CaseSummary`` but not as metadata would raise and 500 the entire list,
    *after* the audit row already recorded a served metadata read. (The
    standalone arm is unaffected: its conversion is best-effort per case inside
    ``CaseService``.)

    Pinning validation parity removes the failure mode instead of catching it,
    so ``from_summary`` needs no second best-effort swallow — which would only
    trade a 500 for a page silently shorter than its own ``total_count``.

    Both halves of "validates" are compared. The annotation is the obvious one;
    ``FieldInfo.metadata`` carries the constraints (``max_length``, ``pattern``,
    ``ge``…) that pydantic applies *on top* of it, and a constraint added here
    alone would narrow the accepted set while leaving annotations equal — the
    exact drift this test exists to catch, invisible to a type-only check.
    """
    # Only fields both models declare; a metadata-only field is the separate
    # concern of ``test_metadata_invents_no_field_of_its_own``, and looking one
    # up here would KeyError instead of failing with a readable message.
    shared = set(AdminCaseMetadata.model_fields) & set(CaseSummary.model_fields)

    def _shape(field):
        return (field.annotation, list(field.metadata))

    mismatched = {
        name: {
            "metadata_row": _shape(AdminCaseMetadata.model_fields[name]),
            "case_summary": _shape(CaseSummary.model_fields[name]),
        }
        for name in sorted(shared)
        if _shape(AdminCaseMetadata.model_fields[name])
        != _shape(CaseSummary.model_fields[name])
    }

    assert not mismatched, (
        "AdminCaseMetadata has drifted from CaseSummary — a row that validates "
        f"upstream may fail the cloud projection: {mismatched}"
    )


def test_metadata_invents_no_field_of_its_own():
    """Every metadata field is a real ``CaseSummary`` field.

    Keeps ``from_summary`` a pure projection: a field here that ``CaseSummary``
    lacks would have to be sourced from somewhere else, which is how a second,
    divergent read path starts.
    """
    invented = set(AdminCaseMetadata.model_fields) - set(CaseSummary.model_fields)
    assert (
        not invented
    ), f"AdminCaseMetadata declares unknown field(s): {sorted(invented)}"


def test_content_fields_are_the_free_text_ones():
    """``CASE_SUMMARY_CONTENT_FIELDS`` names fields that exist and are strings.

    Cheap, but it catches the rename that would otherwise turn the constant into
    a silently empty guard while every test above still passes.
    """
    for name in CASE_SUMMARY_CONTENT_FIELDS:
        assert name in CaseSummary.model_fields, (
            f"CASE_SUMMARY_CONTENT_FIELDS names '{name}', which CaseSummary no "
            "longer has — the content boundary is guarding nothing."
        )
        annotation = CaseSummary.model_fields[name].annotation
        # ``str`` or ``Optional[str]`` — either is text a user typed. Matching
        # the bare type only would fail the day a content field goes nullable,
        # which is not a reason to re-examine the boundary.
        assert annotation is str or str in get_args(annotation), (
            f"CASE_SUMMARY_CONTENT_FIELDS names '{name}', which is no longer a "
            f"text field ({annotation}) — reclassify it."
        )
