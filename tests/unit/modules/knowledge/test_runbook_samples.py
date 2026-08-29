"""The shared gate-passing runbook sample really passes the gate.

``tests/runbook_samples.valid_runbook`` exists because ``upload_document``
enforces ``RunbookValidator`` since #1214, so several tests about OTHER things
(on-disk layout, filename containment, the suggestion flow) need content the
gate accepts. If the runbook schema moves and the sample stops passing, those
tests fail with a refusal that has nothing to do with what they assert. This
pins the sample itself, so the schema change lands as one obvious failure here.
"""

from __future__ import annotations

import pytest

from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    RunbookValidator,
)
from tests.runbook_samples import valid_runbook

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]


def test_the_sample_passes_validation():
    result = RunbookValidator().validate_content(valid_runbook())
    assert result.passed, result.errors


def test_the_title_argument_reaches_the_frontmatter():
    """Callers pass a title to keep the on-disk filenames distinct; a sample
    that ignored it would silently collapse them."""
    content = valid_runbook("A Distinctive Runbook Title")
    assert "title: A Distinctive Runbook Title" in content
    assert RunbookValidator().validate_content(content).passed


def test_removing_a_required_section_makes_it_fail():
    """The sample is not passing because the validator is inert."""
    mutated = valid_runbook().replace("## Sources", "## Not Sources")
    result = RunbookValidator().validate_content(mutated)
    assert not result.passed
    assert "Missing required section: Sources" in result.errors
