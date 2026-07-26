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
        assert CaseSummary.model_fields[name].annotation is str
