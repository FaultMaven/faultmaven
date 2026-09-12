"""A document that is already a FaultMaven runbook is refused, not converted.

#1375: ``ALREADY_A_RUNBOOK`` had its enum member, its 422 in
``conversion_routes`` and its Dashboard copy, but no raise site — Stage 1b
appended a warning and set a ``PreprocessingResult`` field nothing read. A
runbook therefore reached the analysis pass, which reads each ``### Cause``
subsection as a separate failure mode and produces one runbook per cause.

Two directions are pinned here, and both matter: every runbook the system
itself considers valid is REFUSED (or the fragmentation comes back), and a
technical document that merely carries frontmatter is NOT (or the refusal
blocks the incident reports this pipeline exists to convert).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from faultmaven.modules.knowledge.domain.models.conversion import ConversionErrorCode
from faultmaven.modules.knowledge.domain.services.document_preprocessor import (
    _RUNBOOK_BODY_SECTIONS,
    DocumentPreprocessor,
    cleanup_text,
    detect_existing_runbook,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    REQUIRED_SECTIONS,
    RunbookValidator,
)
from tests.runbook_samples import valid_runbook

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]


PACK_RUNBOOKS = sorted(
    (Path(__file__).resolve().parents[4] / "resources/knowledge/pack/runbooks").rglob(
        "*.md"
    )
)


# ---------------------------------------------------------------------------
# The gate's section names are the validator's
# ---------------------------------------------------------------------------


def test_body_sections_are_required_sections():
    """The gate checks section names the validator owns, not a private copy.

    ``_RUNBOOK_BODY_SECTIONS`` is spelled out in the preprocessor because it is a
    two-element subset, not the whole list. This is what makes that spelling
    non-drifting: rename a section in ``RunbookValidator`` and this fails, rather
    than the gate quietly matching nothing and every runbook becoming
    convertible again.
    """
    assert set(_RUNBOOK_BODY_SECTIONS) <= set(REQUIRED_SECTIONS)


# ---------------------------------------------------------------------------
# Direction 1: a runbook is detected
# ---------------------------------------------------------------------------


def test_a_runbook_the_validator_accepts_is_detected():
    """Whatever the system will publish as a runbook, it refuses to convert."""
    content = valid_runbook()
    result = RunbookValidator().validate_content(content)
    assert result.passed, f"sample no longer passes the gate: {result.errors}"

    assert detect_existing_runbook(content) is True


@pytest.mark.parametrize("path", PACK_RUNBOOKS, ids=lambda p: p.name)
def test_every_shipped_runbook_is_detected(path: Path):
    """The whole shipped pack — the runbooks a user actually has to hand.

    Checked through ``cleanup_text`` as well: detection runs before cleanup
    today, and this keeps the gate honest if that order ever changes.
    """
    content = path.read_text()
    assert detect_existing_runbook(content) is True
    assert detect_existing_runbook(cleanup_text(content)) is True


def test_pack_is_not_empty():
    """A positive control: an empty glob would make the parametrisation vacuous."""
    assert len(PACK_RUNBOOKS) > 50


# ---------------------------------------------------------------------------
# Direction 2: a document that is not a runbook is not detected
# ---------------------------------------------------------------------------


INCIDENT_REPORT = """---
id: INC-4821
service: checkout-api
severity: high
status: resolved
author: on-call
---

# Incident 4821: Checkout latency

## Summary
p99 latency on POST /checkout rose from 180ms to 9.4s for 42 minutes.

## Timeline
- 14:02 Alert `CheckoutLatencyHigh` fired.
- 14:11 Connection pool saturation confirmed: `ERROR: remaining connection slots are reserved`.
- 14:44 Rolled back deploy 2f81c; latency recovered.

## Causes of the outage
Deploy 2f81c raised the per-pod pool size from 10 to 40 without lowering the
replica count, so aggregate client pools exceeded `max_connections`.

## Follow-up
- Cap aggregate pool size in the Helm chart.
"""


def test_incident_report_with_frontmatter_is_not_detected():
    """Four generic frontmatter fields are not a runbook.

    ``id``/``service``/``severity``/``status`` meets the 4-of-6 frontmatter
    threshold on their own. This document is an incident report — one of the
    source types ``ANALYSIS_SYSTEM_PROMPT`` names — and refusing it would block
    the pipeline's own use case. Note it also carries a ``## Causes of the
    outage`` heading, which the exact-anchored section match must not accept.
    """
    assert detect_existing_runbook(INCIDENT_REPORT) is False


def _split_sample() -> tuple[str, str]:
    """Return the sample's (frontmatter block, body), split on the delimiters."""
    match = re.match(r"^(---\s*\n.*?\n---\s*\n)(.*)$", valid_runbook(), re.DOTALL)
    assert match, "sample no longer opens with a frontmatter block"
    return match.group(1), match.group(2)


def test_runbook_frontmatter_without_the_body_is_not_detected():
    """Frontmatter alone does not decide it."""
    frontmatter, _ = _split_sample()
    assert (
        detect_existing_runbook(
            f"{frontmatter}\n# Notes\n\nSome prose about postgres.\n"
        )
        is False
    )


def test_runbook_body_without_frontmatter_is_not_detected():
    """Nor does the body alone — both halves are required."""
    _, body = _split_sample()
    assert "## Causes" in body
    assert detect_existing_runbook(body) is False


def test_plain_troubleshooting_prose_is_not_detected():
    assert (
        detect_existing_runbook("# Fixing Redis OOM\n\nRun `INFO memory`.\n") is False
    )


# ---------------------------------------------------------------------------
# The refusal reaches the caller
# ---------------------------------------------------------------------------


async def test_preprocess_refuses_a_runbook_with_already_a_runbook(tmp_path):
    """Stage 1b rejects, and does so before any LLM call.

    ``llm_router``/``settings`` are left unset: the triage stage would need
    them, so reaching it at all would raise here rather than pass.
    """
    path = tmp_path / "redis-oom.md"
    path.write_text(valid_runbook())

    result = await DocumentPreprocessor().preprocess(path, "text/markdown")

    assert result.is_rejected is True
    assert result.error_code == ConversionErrorCode.ALREADY_A_RUNBOOK
    assert "already a FaultMaven runbook" in result.rejection_reason
    # The refusal carries no document text onward.
    assert result.extracted_text == ""


async def test_preprocess_does_not_refuse_an_incident_report(tmp_path):
    """The negative control on the same seam: this one proceeds past Stage 1b.

    It is stopped later (no triage router is wired, so the run ends at the
    stages that need one) — what matters is that it is not stopped HERE.
    """
    path = tmp_path / "INC-4821.md"
    path.write_text(INCIDENT_REPORT)

    result = await DocumentPreprocessor().preprocess(path, "text/markdown")

    assert result.error_code != ConversionErrorCode.ALREADY_A_RUNBOOK


# ---------------------------------------------------------------------------
# The refusal is reachable from the HTTP surface
# ---------------------------------------------------------------------------


def _app_with_real_service():
    """Mount the route over a ConversionService that really preprocesses.

    The point of #1375 was that ``ALREADY_A_RUNBOOK`` had a status-map entry and
    Dashboard copy but no raise site — a map entry nothing reaches. A double in
    place of the service would re-create exactly that: it would assert the map,
    not that the map is reachable. So the service here is a real instance with a
    real ``DocumentPreprocessor``.

    It is deliberately built with no ``_llm_router``, which makes the assembly
    an ordering oracle as well: if the refusal ever stops firing, the request
    runs on to the analysis call and raises ``AttributeError`` there, so the
    test fails with a 500 rather than quietly passing through a later refusal
    that happens to share the status code.
    """
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from faultmaven.config.settings import get_settings
    from faultmaven.modules.knowledge.api.conversion_routes import (
        _get_conversion_service,
        _require_auth,
    )
    from faultmaven.modules.knowledge.api.conversion_routes import (
        router as conversion_router,
    )
    from faultmaven.modules.knowledge.domain.services.conversion_service import (
        ConversionService,
    )

    service = ConversionService.__new__(ConversionService)
    service._settings = get_settings()
    service._preprocessor = DocumentPreprocessor()

    async def _ensure_team_publish_allowed(*_args, **_kwargs):
        return None

    service._ensure_team_publish_allowed = _ensure_team_publish_allowed

    app = FastAPI()
    app.include_router(conversion_router, prefix="/api/v1")

    async def _service():
        return service

    async def _user():
        return SimpleNamespace(
            user_id="user-1",
            organization_id="org-1",
            enterprise_id="ent-1",
            is_platform_admin=lambda: False,
        )

    app.dependency_overrides[_get_conversion_service] = _service
    app.dependency_overrides[_require_auth] = _user
    return TestClient(app)


def test_post_convert_answers_422_already_a_runbook():
    """End to end: uploading a runbook to /knowledge/convert is a 422."""
    client = _app_with_real_service()

    response = client.post(
        "/api/v1/knowledge/convert",
        # "personal" keeps the request clear of the global-authoring admin gate.
        data={"scope": "personal"},
        files={"file": ("redis-oom.md", valid_runbook(), "text/markdown")},
    )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error_code"] == ConversionErrorCode.ALREADY_A_RUNBOOK
    assert "already a FaultMaven runbook" in body["detail"]
