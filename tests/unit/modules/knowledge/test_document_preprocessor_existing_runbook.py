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
from faultmaven.modules.knowledge.domain.services.document_parser import DocumentParser
from faultmaven.modules.knowledge.domain.services.document_preprocessor import (
    _RUNBOOK_BODY_SECTIONS,
    _RUNBOOK_FRONTMATTER_FIELDS,
    _RUNBOOK_FRONTMATTER_MIN_FIELDS,
    DocumentPreprocessor,
    cleanup_text,
    detect_existing_runbook,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    REQUIRED_METADATA,
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


def test_frontmatter_fields_are_required_metadata():
    """The OTHER half of the AND needs the same pin, for the same reason.

    Only the section names were pinned at first, which left the frontmatter half
    free to drift: ``REQUIRED_METADATA`` currently carries all six of
    ``_RUNBOOK_FRONTMATTER_FIELDS``, so the 4-of-6 threshold is reachable. Make
    three of them optional in a future schema change and EVERY valid runbook
    falls under the threshold — the gate disarms completely and nothing fails.
    """
    assert set(_RUNBOOK_FRONTMATTER_FIELDS) <= set(REQUIRED_METADATA)
    # …and enough of them survive for the threshold to be satisfiable at all.
    assert (
        len(set(_RUNBOOK_FRONTMATTER_FIELDS) & set(REQUIRED_METADATA))
        >= _RUNBOOK_FRONTMATTER_MIN_FIELDS
    )


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
    # Through ``DocumentParser``, which is what production feeds the gate —
    # not ``read_text``. ``_extract_markdown`` strips HTML comments before
    # detection ever runs, so a pack runbook that later ships a ``<!-- … -->``
    # directive across a section heading would break the real path while a
    # read_text-based test kept passing.
    parsed = DocumentParser().parse(path, "text/markdown")
    assert detect_existing_runbook(parsed) is True
    assert detect_existing_runbook(cleanup_text(parsed)) is True


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
    """Stage 1b rejects, before cleanup and before any LLM call.

    The ordering is asserted directly, on ``extracted_text``: a refusal at
    Stage 1b carries no document text onward, whereas every later rejection
    returns the extracted text it had already produced. (``llm_router`` being
    unset is NOT an oracle here — ``_run_content_triage`` returns ``None`` when
    it is missing rather than raising, so the pipeline would have run to
    completion without one.)
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

    Without an LLM router the triage stage is skipped (it returns ``None``), so
    this document runs the pipeline to a non-rejected result. What is asserted
    is only that it was not stopped HERE, by this gate.
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
    with _app_with_real_service() as client:
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


# ---------------------------------------------------------------------------
# Regressions found by review of the first cut of this gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,prefix",
    [
        ("utf-8 BOM", "\ufeff"),
        ("leading blank line", "\n"),
        ("leading blank lines", "\n\n\n"),
        ("leading spaces", "   "),
        ("BOM then blank line", "\ufeff\n"),
    ],
)
def test_bytes_before_the_frontmatter_do_not_disarm_the_gate(label, prefix):
    """A runbook saved by an editor that emits a BOM is still a runbook.

    The frontmatter anchor is ``re.match``, so it binds at offset 0. A UTF-8 BOM
    — what "UTF-8 with BOM" produces, and what ``Path.read_text(encoding='utf-8')``
    faithfully preserves — or one blank line ahead of the ``---`` made the whole
    gate answer False, and #1375 reproduced in full on a byte-identical file.
    Detection runs before ``cleanup_text``, which is the only thing that would
    otherwise have removed them.
    """
    assert detect_existing_runbook(prefix + valid_runbook()) is True


async def test_bom_encoded_runbook_is_refused_end_to_end(tmp_path):
    """The same thing through the real read path, not just the predicate."""
    path = tmp_path / "redis-oom.md"
    # ``utf-8-sig`` is how a BOM actually arrives: the editor writes it, and
    # nothing between the file and the gate removes it.
    path.write_text(valid_runbook(), encoding="utf-8-sig")

    result = await DocumentPreprocessor().preprocess(path, "text/markdown")

    assert result.is_rejected is True
    assert result.error_code == ConversionErrorCode.ALREADY_A_RUNBOOK


POSTMORTEM_QUOTING_A_RUNBOOK = """---
id: INC-4821
service: checkout-api
severity: high
status: resolved
---

# Incident 4821: Checkout latency

## Timeline
- 14:11 Connection pool saturation confirmed.

## What we ran
We followed the connection-pool runbook, reproduced here for the record:

```markdown
## Symptom Recognition
- "ERROR: remaining connection slots are reserved"

## Causes
### Cause A: idle-in-transaction sessions hold their slots
```

## Follow-up
- Cap aggregate pool size in the Helm chart.
"""


def test_a_postmortem_quoting_a_runbook_is_not_detected():
    """Quoting the runbook you ran is routine in a postmortem — and convertible.

    This document meets the frontmatter threshold on four generic fields, and
    its fenced quote carries both canonical section headings. Searching the raw
    text finds them and hard-refuses with 422 the exact document class the body
    requirement was added to protect. The fence mask is what separates "is a
    runbook" from "talks about one".
    """
    assert detect_existing_runbook(POSTMORTEM_QUOTING_A_RUNBOOK) is False


def test_fence_masking_preserves_line_structure():
    """Masking to blank lines, not deleting — deletion can manufacture a match.

    Deleting a fence splices the lines either side together. Here that would
    join a bare ``##`` to ``Causes``, producing a heading the source never had
    (the splice hazard #1241 hit when it deleted comments instead of masking).
    """
    spliced = (
        valid_runbook().split("## Causes")[0]
        + "##"
        + "\n```\nfenced\n```\n"
        + " Causes\n\n### Cause A: x\n"
    )
    assert detect_existing_runbook(spliced) is False


async def test_near_runbook_still_warns(tmp_path):
    """Frontmatter but no recognised body: convertible, but say why it may split.

    The tightened gate made this case silent — it previously carried the
    "appears to already be a FaultMaven runbook" advisory. It still converts,
    and fragmentation is still the likely outcome, so the advisory is the right
    severity for the half that cannot carry a refusal alone.
    """
    frontmatter, _ = _split_sample()
    path = tmp_path / "flattened-runbook.md"
    path.write_text(
        f"{frontmatter}\n# Runbook\n\nSymptoms: the pool is exhausted and "
        "`ERROR: remaining connection slots are reserved` appears in the log.\n\n"
        "Run `SELECT count(*) FROM pg_stat_activity;` to check.\n"
    )

    result = await DocumentPreprocessor().preprocess(path, "text/markdown")

    assert result.error_code != ConversionErrorCode.ALREADY_A_RUNBOOK
    assert any("runbook frontmatter" in w for w in result.warnings), result.warnings


def test_incident_report_gets_no_near_runbook_warning():
    """The near-runbook warning must not fire on an incident report.

    This is where restoring the warning could have re-introduced, as noise, the
    very false positive the body requirement removed from the 422: the incident
    report meets the 4-of-6 frontmatter threshold on generic fields, so a
    warning keyed on that half alone fires on every one of them.
    ``is_near_runbook`` keys on ``symptom_class`` as well, which no tracker
    export carries.
    """
    from faultmaven.modules.knowledge.domain.services.document_preprocessor import (
        _has_runbook_frontmatter,
        is_near_runbook,
    )

    # The threshold IS met — that is the point; the warning still must not fire.
    assert _has_runbook_frontmatter(INCIDENT_REPORT) is True
    assert is_near_runbook(INCIDENT_REPORT) is False


def test_a_full_runbook_is_not_a_near_runbook():
    """A runbook the gate refuses outright is not also warned about."""
    from faultmaven.modules.knowledge.domain.services.document_preprocessor import (
        is_near_runbook,
    )

    assert is_near_runbook(valid_runbook()) is False
