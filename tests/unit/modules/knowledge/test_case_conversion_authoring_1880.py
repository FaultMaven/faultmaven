"""#1880 — a case becomes a runbook under ONE authoring policy, from shared code.

A case names an incident; a runbook is reusable knowledge. The live
case→runbook path (``convert_from_case``) used to take its ``service`` from
``ProblemVerification.affected_services`` — the user's own service, which no
runbook carries and nothing wrote, so every case runbook said
``service: unknown`` — and to pre-compute its id and its draft title from the
case title, which carried the incident into both
(``unknown-checkout-500s-after-deploy``). The sibling extraction path already
had the policy that prevents both: the model writes a de-identified title and
infers the technology, and the id is minted from the frontmatter it produced.
Both paths now render that policy from ``case_authoring`` and mint through it.

Pinned here, with the knowledge model stubbed (no live LLM call):

1. **The request has no service input path.** ``CaseConversionRequest`` has no
   ``service`` (or ``tags``) field, and the failure mode the case path builds
   carries an empty ``service`` — never a value read off the case.
2. **The model is told to infer, under the shared rules.** No ``RUNBOOK_ID``
   and no case-supplied ``SERVICE`` or ``FAILURE MODE`` value reach it ahead
   of the source material, and the three ``case_authoring`` rules are rendered
   verbatim.
3. **The id and the draft's title come from the produced frontmatter**, through
   the shared mint, so a noisy case title reaches neither.
4. **The extraction prompt is byte-identical** to its pre-#1880 render: moving
   its rules into ``case_authoring`` changed where they live, not what the
   model reads.
"""

import string
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.llm.providers import LLMResponse, StopReason
from faultmaven.modules.case.domain.models.problem import ProblemVerification
from faultmaven.modules.knowledge.domain.case_authoring import (
    CASE_ID_RULE,
    DE_IDENTIFICATION_RULES,
    TECHNOLOGY_RULE,
    draft_title,
    mint_case_runbook_id,
)
from faultmaven.modules.knowledge.domain.models.conversion import (
    CaseConversionRequest,
    ConversionStatus,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.modules.knowledge.domain.services.suggestion_service import (
    SuggestionService,
)
from faultmaven.utils.runbook_id import draft_filename
from tests.runbook_samples import valid_runbook

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

CASE_ID = "case_aa0000001880"
NOISY_TITLE = "INC-48213 prod-web-07 checkout 500s"
#: What the stubbed model writes: a de-identified failure-mode title, and the
#: technology in ``service`` (``valid_runbook`` carries ``postgresql``).
PRODUCED_TITLE = "PostgreSQL Connection Pool Exhaustion"
LEAKS = ("inc-48213", "prod-web-07", "checkout", "unknown")

GOLDEN = (
    Path(__file__).parent
    / "fixtures"
    / "extraction_prompt"
    / "rendered_before_1880.txt"
)


def _resolved_case():
    """A resolved case whose title and statement name the incident."""
    return SimpleNamespace(
        case_id=CASE_ID,
        title=NOISY_TITLE,
        problem_verification=ProblemVerification(
            symptom_statement="checkout on prod-web-07 returns 500s", severity="HIGH"
        ),
        root_cause_conclusion=SimpleNamespace(
            root_cause="PostgreSQL connection pool exhausted by leaked connections",
            mechanism=None,
            contributing_factors=[],
        ),
    )


def _response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=100,
        response_time_ms=10,
        stop_reason=StopReason.STOP,
    )


async def _convert(tmp_path, produced: str):
    """Run ``convert_from_case`` end to end with the model stubbed; return the
    response and the user message the model was sent."""
    router = AsyncMock()
    router.route.return_value = _response(produced)
    settings = MagicMock()
    settings.llm.get_knowledge_model.return_value = "test-model"
    service = ConversionService(
        llm_router=router,
        settings=settings,
        db_session_factory=None,
        knowledge_service=None,
    )
    request = CaseConversionRequest.from_case(_resolved_case())
    with patch.object(
        ConversionService,
        "_data_dir",
        new_callable=lambda: property(lambda self: tmp_path),
    ):
        response = await service.convert_from_case(
            request, user_id="u_1880", enterprise_id=None
        )
    messages = router.route.await_args.kwargs["messages"]
    user_message = next(m["content"] for m in messages if m["role"] == "user")
    return response, user_message


# ---------------------------------------------------------------------------
# 1. No service input path
# ---------------------------------------------------------------------------


def test_the_request_has_no_service_or_tags_field():
    assert "service" not in CaseConversionRequest.model_fields
    assert "tags" not in CaseConversionRequest.model_fields


def test_from_case_supplies_no_service():
    request = CaseConversionRequest.from_case(_resolved_case())
    dumped = request.model_dump()
    assert "service" not in dumped
    assert "tags" not in dumped


# ---------------------------------------------------------------------------
# 2-3. End to end: what the model is told, and where the id comes from
# ---------------------------------------------------------------------------


async def test_the_model_is_told_to_infer_under_the_shared_rules(tmp_path):
    _, message = await _convert(tmp_path, valid_runbook(PRODUCED_TITLE))
    instructions, _, source = message.partition("--- SOURCE MATERIAL ---")

    assert "RUNBOOK_ID:" not in instructions
    assert (
        "SERVICE: (not supplied for a case — infer it from the source material)\n"
        in instructions
    )
    for rule in (CASE_ID_RULE, TECHNOLOGY_RULE, DE_IDENTIFICATION_RULES):
        assert rule in instructions
    # The case title is source material, never an instruction to copy.
    assert "INC-48213" not in instructions
    assert f"CASE TITLE: {NOISY_TITLE}" in source


async def test_the_id_and_title_come_from_the_produced_frontmatter(tmp_path):
    produced = valid_runbook(PRODUCED_TITLE)
    response, _ = await _convert(tmp_path, produced)

    assert response.status == ConversionStatus.COMPLETED
    (draft,) = response.drafts
    assert draft.runbook_id == mint_case_runbook_id(produced, CASE_ID)
    assert draft.title == draft_title(produced) == PRODUCED_TITLE
    for leaked in LEAKS:
        assert leaked not in draft.runbook_id, draft.runbook_id
        assert leaked not in draft.title.lower(), draft.title
    # The frontmatter, the file and the row agree on the one id.
    assert f"\nid: {draft.runbook_id}\n" in draft.content
    assert Path(draft.file_path).name == draft_filename(draft.runbook_id)
    assert Path(draft.file_path).read_text(encoding="utf-8") == draft.content


async def test_the_case_failure_mode_carries_no_service(tmp_path):
    response, _ = await _convert(tmp_path, valid_runbook(PRODUCED_TITLE))
    (failure_mode,) = response.analysis.failure_modes
    assert failure_mode.service == ""


async def test_a_draft_with_no_title_falls_back_to_the_case_stem(tmp_path):
    produced = valid_runbook(PRODUCED_TITLE).replace(f"title: {PRODUCED_TITLE}\n", "")
    response, _ = await _convert(tmp_path, produced)

    (draft,) = response.drafts
    assert draft.runbook_id == mint_case_runbook_id(produced, CASE_ID)
    assert draft.runbook_id.startswith("case-")
    # Named by its id, never by the case title.
    assert draft.title == draft.runbook_id


# ---------------------------------------------------------------------------
# 4. The extraction prompt did not move under the constant move
# ---------------------------------------------------------------------------


def _sentinel_render(template: str) -> str:
    fields = {f for _, f, _, _ in string.Formatter().parse(template) if f}
    return template.format(**{f: f"<{f}>" for f in fields})


def test_the_extraction_prompt_renders_byte_identically_to_before_the_move():
    assert _sentinel_render(SuggestionService.EXTRACTION_PROMPT) == GOLDEN.read_text(
        encoding="utf-8"
    )


def test_the_extraction_prompt_renders_the_shared_rules():
    for rule in (CASE_ID_RULE, TECHNOLOGY_RULE, DE_IDENTIFICATION_RULES):
        assert rule in SuggestionService.EXTRACTION_PROMPT
