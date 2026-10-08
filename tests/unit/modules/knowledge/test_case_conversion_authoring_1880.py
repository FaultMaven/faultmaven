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
4. **The extraction prompt is pinned byte for byte** to a golden render of
   the shared rules. Moving them into ``case_authoring`` changed nothing the
   model reads; the one deliberate change since is the internal-service-name
   line #1900's review added to ``DE_IDENTIFICATION_RULES``, and the fixture
   moved with it.
5. **One predicate says what a title is.** The rule-8 placeholder and a
   punctuation-only title are no title, for the mint and for the draft's name
   alike, on both paths.
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
    case_disambiguated_runbook_id,
    case_stem_runbook_id,
    draft_title,
    mint_case_runbook_id,
    usable_title,
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
from faultmaven.modules.knowledge.infrastructure.persistence.suggestion_repository import (  # noqa: E501
    InMemorySuggestionRepository,
)
from faultmaven.utils.runbook_id import draft_filename
from tests.runbook_samples import valid_runbook
from tests.utils import case_repository_holding

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
    / "rendered_with_case_authoring_rules.txt"
)

#: The line #1900's review added to ``DE_IDENTIFICATION_RULES``: the user's
#: own service ("checkout") is an incident identifier, not a product name.
SERVICE_NAME_RULE_LINE = (
    "- internal service, application and team names (describe the role each played:"
)

#: Frontmatter titles that are no title (``usable_title``), quoted as the
#: rule-8 skeleton quotes its placeholder (unquoted, ``[...]`` is a YAML list
#: and ``!!!`` a YAML tag).
NOT_A_TITLE = {
    "placeholder": '"[INSUFFICIENT SOURCE DATA -- manual completion required]"',
    "punctuation-only": '"!!!"',
}


def _titled(raw_title: str) -> str:
    return valid_runbook(PRODUCED_TITLE).replace(
        f"title: {PRODUCED_TITLE}\n", f"title: {raw_title}\n"
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


async def test_the_case_path_tells_the_model_to_remove_internal_service_names(
    tmp_path,
):
    _, message = await _convert(tmp_path, valid_runbook(PRODUCED_TITLE))
    instructions, _, _ = message.partition("--- SOURCE MATERIAL ---")
    assert SERVICE_NAME_RULE_LINE in instructions


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


def test_the_extraction_prompt_renders_byte_identically_to_its_golden():
    assert _sentinel_render(SuggestionService.EXTRACTION_PROMPT) == GOLDEN.read_text(
        encoding="utf-8"
    )


def test_the_extraction_prompt_renders_the_shared_rules():
    for rule in (CASE_ID_RULE, TECHNOLOGY_RULE, DE_IDENTIFICATION_RULES):
        assert rule in SuggestionService.EXTRACTION_PROMPT


# ---------------------------------------------------------------------------
# 5. One "usable title" predicate, both helpers, both paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw_title", NOT_A_TITLE.values(), ids=NOT_A_TITLE.keys())
def test_the_shared_helpers_agree_that_it_is_no_title(raw_title):
    produced = _titled(raw_title)
    assert usable_title(raw_title.strip('"')) is None
    assert draft_title(produced) is None
    assert mint_case_runbook_id(produced, CASE_ID) == case_stem_runbook_id(CASE_ID)


def test_a_real_title_is_usable_and_stripped():
    assert usable_title(f"  {PRODUCED_TITLE} ") == PRODUCED_TITLE


@pytest.mark.parametrize("raw_title", NOT_A_TITLE.values(), ids=NOT_A_TITLE.keys())
async def test_the_case_path_names_such_a_draft_by_the_case_stem(tmp_path, raw_title):
    response, _ = await _convert(tmp_path, _titled(raw_title))

    (draft,) = response.drafts
    assert draft.runbook_id == case_stem_runbook_id(CASE_ID)
    assert draft.title == draft.runbook_id
    assert f"\nid: {draft.runbook_id}\n" in draft.content


class _SameBodyProvider:
    """Returns one body on every call: the extraction loop retries a draft the
    gate refuses, and a ``!!!`` title is short enough to be refused."""

    def __init__(self, body: str):
        self.body = body

    async def generate(self, *, prompt: str, **kwargs) -> SimpleNamespace:
        return SimpleNamespace(content=self.body, is_truncated=False)


@pytest.mark.parametrize("raw_title", NOT_A_TITLE.values(), ids=NOT_A_TITLE.keys())
async def test_the_extraction_path_mints_such_a_draft_by_the_case_stem(raw_title):
    service = SuggestionService(
        case_repository=case_repository_holding(
            CASE_ID, enterprise_id="ent_1880", title=NOISY_TITLE
        ),
        knowledge_service=MagicMock(),
        sanitizer=None,
        llm_provider=_SameBodyProvider(_titled(raw_title)),
        suggestion_repository=InMemorySuggestionRepository(),
    )
    suggestion = await service.extract_knowledge_from_case(
        case_id=CASE_ID, enterprise_id="ent_1880", extracted_by="u_1880"
    )

    assert f"\nid: {case_stem_runbook_id(CASE_ID)}\n" in suggestion.suggested_content
    # The draft's "title" was not taken as its name either.
    assert "!!!" not in suggestion.suggested_title
    assert "INSUFFICIENT" not in suggestion.suggested_title


# ---------------------------------------------------------------------------
# The case-path re-mint (the slot itself is exercised on SQLite in
# tests/integration/modules/knowledge/test_case_ids_across_cases_1880.py)
# ---------------------------------------------------------------------------


def test_the_re_mint_appends_the_case_stem_through_the_shared_mint():
    minted = "postgresql-redis-oom-kills"
    assert (
        case_disambiguated_runbook_id(minted, CASE_ID)
        == f"{minted}-{case_stem_runbook_id(CASE_ID)}"
    )


def test_a_long_minted_id_is_still_separated_by_the_case_stem():
    minted = mint_case_runbook_id(valid_runbook(PRODUCED_TITLE), CASE_ID)
    other = "case_bb0000001880"
    ours = case_disambiguated_runbook_id(minted, CASE_ID)
    theirs = case_disambiguated_runbook_id(minted, other)
    assert len({minted, ours, theirs}) == 3


def test_the_case_stem_is_never_re_minted():
    assert case_disambiguated_runbook_id(case_stem_runbook_id(CASE_ID), CASE_ID) is None
