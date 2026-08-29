"""The extraction path aims at the publication gate, not past it (#1226).

Since #1214 ``upload_document`` enforces ``RunbookValidator`` before its first
side effect, so a suggestion approved without a human edit publishes only if the
extractor's output CLEARS that gate. It did not: ``EXTRACTION_PROMPT`` asked for
``## Problem / ## Root Cause / ## Solution / ## Prevention``, which the validator
refuses with six errors, so every approval was a 422 and the extract → review →
approve loop completed only through a manual reshape.

Four things are pinned here, one per way the fix could rot:

1. **The prompt asks for the v4 schema** — and asks for it by REUSING the one
   authoring prompt the vocabulary drift-guard already pins, rather than
   carrying a second copy that can drift from the validator.
2. **The retry fires, and feeds the errors back.** A repair turn that re-sends
   the original prompt is not a repair turn; the assertion is on the errors
   appearing in the second prompt, not merely on a second call happening.
3. **A draft that still fails reaches the reviewer with the reasons attached**,
   and the same verdict is recomputed when the reviewer edits — that loop is
   the whole point of surfacing it.
4. **The gate is not weakened.** Genuinely invalid content is still refused,
   and the no-LLM skeleton — v4-shaped so the reviewer edits in the right
   schema — is still unpublishable.

The measured before/after pass rate against a live model is not here (a CI test
may not call one): it is ``tests/eval/suggestion_extraction/``.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.knowledge.domain.models.suggestion import (
    KnowledgeSuggestion,
    PIIScanStatus,
)
from faultmaven.modules.knowledge.domain.services import conversion_service
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    VALID_DOMAINS,
    RunbookValidator,
)
from faultmaven.modules.knowledge.domain.services.suggestion_service import (
    SuggestionService,
)
from tests.runbook_samples import valid_runbook

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

ORG = "org_1226"
CASE_ID = "case_aabb11223344"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class ScriptedProvider:
    """Returns queued bodies in order and records the prompt of every call.

    A real ``LLMResponse`` shape (``content`` + ``is_truncated``) because the
    service reads both: a ``MagicMock`` would report ``is_truncated`` truthy and
    send every draft down the truncation branch.
    """

    def __init__(self, *bodies: str):
        self._bodies = list(bodies)
        self.prompts: list[str] = []

    async def generate(self, *, prompt: str, **kwargs) -> SimpleNamespace:
        self.prompts.append(prompt)
        body = self._bodies.pop(0) if self._bodies else self._bodies_exhausted()
        return SimpleNamespace(content=body, is_truncated=False)

    @staticmethod
    def _bodies_exhausted():
        raise AssertionError("provider called more times than the test scripted")


class RedactingSanitizer:
    """A sanitizer that actually REWRITES.

    Every other double in this suite, and the eval driver, pass
    ``sanitizer=None`` — which takes ``_scan_for_pii``'s ``else`` branch and
    never touches the content. That is why the concatenation bug survived a
    green suite AND a 8/8 measured pass rate: nothing in the lane exercised the
    branch that runs in cloud, over case transcripts, which is precisely where
    PII lives.
    """

    def __init__(self, needle: str = "prod-db-01", replacement: str = "<HOST>"):
        self.needle = needle
        self.replacement = replacement
        self.seen: list[str] = []

    async def asanitize(self, text: str) -> str:
        self.seen.append(text)
        return text.replace(self.needle, self.replacement)


def _without_id_line(content: str) -> str:
    """``content`` minus its frontmatter ``id:`` line.

    Every draft has its id re-minted by ``_force_frontmatter_id``, so a
    byte-equality assertion against the input would be testing the mint, not
    the thing under test."""
    return "\n".join(
        line for line in content.splitlines() if not line.startswith("id:")
    ).strip()


def _case_repository(
    title: str = "Checkout API 500s during the evening peak",
) -> MagicMock:
    repo = MagicMock()

    async def get_by_id(case_id):
        return SimpleNamespace(title=title, description="Pool exhausted at peak.")

    async def get_messages(case_id):
        return [SimpleNamespace(role="user", content="checkout is 500ing")]

    async def get_evidence(case_id):
        return []

    repo.get_by_id = get_by_id
    repo.get_messages = get_messages
    repo.get_evidence = get_evidence
    return repo


def _service(provider=None, **kwargs) -> SuggestionService:
    return SuggestionService(
        case_repository=_case_repository(),
        knowledge_service=MagicMock(),
        sanitizer=None,
        llm_provider=provider,
        **kwargs,
    )


async def _extract(svc: SuggestionService) -> KnowledgeSuggestion:
    return await svc.extract_knowledge_from_case(
        case_id=CASE_ID, organization_id=ORG, extracted_by="user_extractor"
    )


# A draft that is NOT a v4 runbook — the shape the prompt used to ask for.
LEGACY_SHAPED_DRAFT = """## Problem
The connection pool is exhausted at peak.

## Root Cause
A transaction is left open on the early-return path.

## Solution
1. Roll back in a finally block.

## Prevention
- Set idle_in_transaction_session_timeout.
"""


# ---------------------------------------------------------------------------
# 1. The prompt asks for the v4 schema, by reusing the authoring prompt
# ---------------------------------------------------------------------------


class TestThePromptAsksForV4:
    async def test_the_generation_prompt_carries_the_v4_template(self):
        """Not 'mentions a runbook' — carries the template the validator
        enforces, section headings and cause sub-fields included."""
        provider = ScriptedProvider(valid_runbook())
        await _extract(_service(provider))

        prompt = provider.prompts[0]
        for section in (
            "## Symptom Recognition",
            "## Applicability",
            "## Diagnostic Steps",
            "## Causes",
            "## Prevention",
            "## Sources",
        ):
            assert section in prompt, f"prompt omits {section}"
        for subfield in ("**Statement:**", "**Indicators:**", "**Interventions:**"):
            assert subfield in prompt, f"prompt omits {subfield}"
        assert "[Default]" in prompt
        assert "last_updated" in prompt and "symptom_class" in prompt

    async def test_it_reuses_the_shared_authoring_prompt_rather_than_copying_it(self):
        """A second copy of the template is a second thing to keep in step with
        the validator. ``test_cause_grammar_vocab`` pins exactly one of them
        against ``cause_grammar``; this asserts the extraction path sits behind
        that same pin instead of beside it.

        Both halves matter: the shipped prompt IS the shared one (the composed
        prompt begins with it, byte for byte), and the module-level constant
        does NOT restate it (a copy pasted into ``EXTRACTION_PROMPT`` would
        satisfy the first assertion while re-opening the drift)."""
        provider = ScriptedProvider(valid_runbook())
        svc = _service(provider)
        await _extract(svc)

        shared = conversion_service.CONVERSION_SYSTEM_PROMPT
        assert provider.prompts[0].startswith(shared)
        assert shared not in svc.EXTRACTION_PROMPT

    async def test_the_prompt_pins_the_scope_the_extractor_owns(self):
        """Approval publishes at the platform tier, so ``scope`` is stated
        rather than inferred."""
        provider = ScriptedProvider(valid_runbook())
        await _extract(_service(provider))

        assert (
            "The frontmatter `scope` field MUST be exactly: global"
            in provider.prompts[0]
        )

    async def test_a_non_kebab_id_from_the_model_is_overwritten(self):
        """Models routinely echo a title into ``id``, and a non-kebab ``id`` is
        a gate error — so it is normalised rather than requested and hoped for.
        The value comes from the draft's own ``service`` + ``title``, the same
        mint the conversion path uses."""
        provider = ScriptedProvider(
            valid_runbook().replace("id: sample-runbook", "id: Checkout API 500s")
        )
        suggestion = await _extract(_service(provider))

        assert "id: postgresql-sample-runbook-for-publication" in (
            suggestion.suggested_content
        )
        assert suggestion.validation_passed is True

    async def test_the_id_is_never_minted_from_the_case_title(self):
        """Measured, not reasoned about (see ``_mint_id``): the first cut minted
        from the case title, and the eval's noisy fixture produced a body the
        model had de-identified perfectly beside a frontmatter line reading
        ``id: case-inc-48213-prod-web-07-returning-502-for-customer-c-…``. The
        id sits inside the content, so it is chunked, embedded and retrieved."""
        svc = SuggestionService(
            case_repository=_case_repository(
                title="INC-48213: prod-web-07 returning 502 for customer Contoso"
            ),
            knowledge_service=MagicMock(),
            sanitizer=None,
            llm_provider=ScriptedProvider(valid_runbook()),
        )
        suggestion = await _extract(svc)

        id_line = next(
            line
            for line in suggestion.suggested_content.splitlines()
            if line.startswith("id:")
        )
        for leaked in ("inc-48213", "prod-web-07", "contoso"):
            assert leaked not in id_line.lower(), id_line

    async def test_the_suggestion_title_is_the_drafts_not_the_cases(self):
        """Same leak class, one field over. ``suggested_title`` is what
        ``upload_document`` publishes as the knowledge item's title AND what
        ``runbook_filename`` names the file after, and deriving it from the case
        title carried the incident into both — "Troubleshooting: INC-48213:
        prod-web-07 returning 502 for customer Contoso…"."""
        svc = SuggestionService(
            case_repository=_case_repository(
                title="INC-48213: prod-web-07 returning 502 for customer Contoso"
            ),
            knowledge_service=MagicMock(),
            sanitizer=None,
            llm_provider=ScriptedProvider(valid_runbook()),
        )
        suggestion = await _extract(svc)

        assert suggestion.suggested_title == "Sample Runbook For Publication"

    async def test_an_explicit_title_still_wins(self):
        """The caller's title is a decision, not a guess."""
        svc = _service(ScriptedProvider(valid_runbook()))
        suggestion = await svc.extract_knowledge_from_case(
            case_id=CASE_ID,
            organization_id=ORG,
            extracted_by="user_extractor",
            title_suggestion="A Title The Reviewer Chose",
        )

        assert suggestion.suggested_title == "A Title The Reviewer Chose"

    async def test_the_skeletons_placeholder_title_is_not_used_as_a_name(self):
        """``[INSUFFICIENT SOURCE DATA…]`` is a form field, not a heading; the
        review inbox learns more from the case title."""
        suggestion = await _extract(_service(provider=None))

        assert "INSUFFICIENT" not in suggestion.suggested_title
        assert suggestion.suggested_title.startswith("Troubleshooting: ")

    async def test_the_prompt_names_the_domain_vocabulary(self):
        """The document path is HANDED a domain by its analysis pass and told
        not to change it; a case supplies none, so the model free-picks. Before
        the vocabulary was named, 4 of 8 first drafts died on `kubernetes` /
        `cache` / `web` — each a hard gate error (eval, --attempts 1)."""
        provider = ScriptedProvider(valid_runbook())
        await _extract(_service(provider))

        prompt = provider.prompts[0]
        for domain in VALID_DOMAINS:
            assert domain in prompt, f"prompt omits domain {domain!r}"
        assert "`domain` MUST be one of:" in prompt

    async def test_the_prompt_forbids_a_multi_step_indicator_token(self):
        """``[Step 2, Step 3]`` is not a token — INDICATOR_TOKEN_RE does not
        match it — and one first draft died on exactly that."""
        provider = ScriptedProvider(valid_runbook())
        await _extract(_service(provider))

        assert "[Step 2, Step 3]" in provider.prompts[0]

    async def test_an_overlong_title_still_mints_a_kebab_id(self):
        """``runbook_id_from_parts`` truncates to 55 chars then appends
        ``-<md5>``, so a slug whose 55th character is a hyphen yields a DOUBLE
        hyphen the validator rejects. Measured: the eval produced
        ``tls-outbound-tls-failure-due-to-expired-ca-certificate--7217``."""
        long_title = "Outbound TLS Failure Due to Expired CA Certificate in Trust"
        draft = valid_runbook().replace("service: postgresql", "service: tls")
        draft = draft.replace(
            "title: Sample Runbook For Publication", f"title: {long_title}"
        )
        provider = ScriptedProvider(draft)
        suggestion = await _extract(_service(provider))

        id_line = next(
            line
            for line in suggestion.suggested_content.splitlines()
            if line.startswith("id:")
        )
        minted = id_line.split(":", 1)[1].strip()
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", minted), minted
        assert suggestion.validation_passed is True, suggestion.validation_errors

    async def test_a_draft_with_no_usable_title_falls_back_to_the_case_stem(self):
        """An empty slug would otherwise mean no ``id`` at all. The case
        identifier is opaque but internal, so it is safe to publish — unlike the
        case title."""
        provider = ScriptedProvider(valid_runbook().replace("title: ", "title2: "))
        suggestion = await _extract(_service(provider))

        assert f"id: case-{CASE_ID}".replace("_", "-") in (suggestion.suggested_content)

    async def test_a_v4_draft_passes_the_same_gate_approval_applies(self):
        provider = ScriptedProvider(valid_runbook())
        suggestion = await _extract(_service(provider))

        assert RunbookValidator().validate_content(suggestion.suggested_content).passed
        assert suggestion.validation_passed is True
        assert suggestion.validation_errors == []


# ---------------------------------------------------------------------------
# 2. The retry fires and feeds the validator's errors back
# ---------------------------------------------------------------------------


class TestTheValidationAwareRetry:
    async def test_a_failing_first_draft_is_retried(self):
        provider = ScriptedProvider(LEGACY_SHAPED_DRAFT, valid_runbook())
        suggestion = await _extract(_service(provider))

        assert len(provider.prompts) == 2
        assert suggestion.validation_passed is True

    async def test_the_repair_turn_carries_the_structured_errors(self):
        """The whole point of using ``ValidationResult.errors``: the model is
        told which lines are wrong, not handed the schema again. A repair turn
        that merely re-sends the original prompt would pass a call-count
        assertion and fail this one."""
        provider = ScriptedProvider(LEGACY_SHAPED_DRAFT, valid_runbook())
        await _extract(_service(provider))

        repair = provider.prompts[1]
        assert "REPAIR REQUIRED" in repair
        assert "No YAML frontmatter found" in repair
        assert "Missing required section: Causes" in repair
        # And the draft being repaired, or there is nothing to repair.
        assert "## Root Cause" in repair

    async def test_a_passing_first_draft_costs_exactly_one_call(self):
        """The retry is a recovery path, not a tax on every extraction."""
        provider = ScriptedProvider(valid_runbook())
        await _extract(_service(provider))

        assert len(provider.prompts) == 1

    async def test_the_budget_is_bounded(self):
        """Two attempts by default — see MAX_EXTRACTION_ATTEMPTS. A third
        failing draft is never generated, because the budget stops first."""
        provider = ScriptedProvider(LEGACY_SHAPED_DRAFT, LEGACY_SHAPED_DRAFT)
        suggestion = await _extract(_service(provider))

        assert SuggestionService.MAX_EXTRACTION_ATTEMPTS == 2
        assert len(provider.prompts) == 2
        assert suggestion.validation_passed is False

    async def test_the_budget_is_injectable(self):
        provider = ScriptedProvider(
            LEGACY_SHAPED_DRAFT, LEGACY_SHAPED_DRAFT, valid_runbook()
        )
        suggestion = await _extract(_service(provider, max_extraction_attempts=3))

        assert len(provider.prompts) == 3
        assert suggestion.validation_passed is True


# ---------------------------------------------------------------------------
# 3. What the reviewer gets when the budget runs out
# ---------------------------------------------------------------------------


class TestTheReviewerSeesWhyItWouldBeRefused:
    async def test_the_suggestion_carries_the_validator_errors(self):
        """The issue's steer: surface the errors rather than refuse bare. The
        suggestion still exists and is still reviewable — it just says why
        approval would 422."""
        provider = ScriptedProvider(LEGACY_SHAPED_DRAFT, LEGACY_SHAPED_DRAFT)
        suggestion = await _extract(_service(provider))

        assert suggestion.validation_passed is False
        assert "No YAML frontmatter found" in suggestion.validation_errors
        assert "Missing required section: Causes" in suggestion.validation_errors

    async def test_the_last_draft_is_kept_not_discarded(self):
        """A near-miss runbook plus a list of defects is a far shorter edit than
        the prose the reviewer used to reshape from nothing."""
        provider = ScriptedProvider(LEGACY_SHAPED_DRAFT, LEGACY_SHAPED_DRAFT)
        suggestion = await _extract(_service(provider))

        assert suggestion.suggested_content.strip() == LEGACY_SHAPED_DRAFT.strip()

    async def test_the_api_response_carries_the_verdict(self):
        provider = ScriptedProvider(LEGACY_SHAPED_DRAFT, LEGACY_SHAPED_DRAFT)
        svc = _service(provider)
        suggestion = await _extract(svc)

        detail = svc.to_api_response(suggestion, include_content=True)
        summary = svc.to_api_response(suggestion)

        assert detail["validation"]["passed"] is False
        assert "No YAML frontmatter found" in detail["validation"]["errors"]
        # The inbox is a list, and "why is this blocked" is asked before a row
        # is opened, so the summary carries it too.
        assert summary["validation"]["passed"] is False
        assert summary["validation"]["errors"]

    async def test_an_unevaluated_suggestion_is_null_not_false(self):
        """``None`` means not yet evaluated. Collapsing it into ``False`` would
        make every pre-#1226 suggestion look refused; collapsing it into
        ``True`` would make it look publishable."""
        svc = _service()
        fresh = KnowledgeSuggestion(
            suggestion_id="sug_unevaluated",
            organization_id=ORG,
            case_id=CASE_ID,
            pii_scan_status=PIIScanStatus.CLEAN,
        )
        assert fresh.validation_passed is None
        assert svc.to_api_response(fresh)["validation"]["passed"] is None

    async def test_a_reviewer_edit_re_runs_the_gate(self):
        """The loop the surfaced errors exist to drive: edit, watch them clear.
        A verdict that stuck at extraction time would tell the reviewer their
        fix had not worked."""
        provider = ScriptedProvider(LEGACY_SHAPED_DRAFT, LEGACY_SHAPED_DRAFT)
        svc = _service(provider)
        suggestion = await _extract(svc)
        assert suggestion.validation_passed is False

        edited = await svc.update_suggestion(
            suggestion_id=suggestion.suggestion_id,
            content=valid_runbook(),
            organization_id=ORG,
        )

        assert edited.validation_passed is True
        assert edited.validation_errors == []

    async def test_an_edit_that_breaks_it_flips_the_verdict_back(self):
        provider = ScriptedProvider(valid_runbook())
        svc = _service(provider)
        suggestion = await _extract(svc)
        assert suggestion.validation_passed is True

        edited = await svc.update_suggestion(
            suggestion_id=suggestion.suggestion_id,
            content=valid_runbook().replace("## Sources", "## Not Sources"),
            organization_id=ORG,
        )

        assert edited.validation_passed is False
        assert "Missing required section: Sources" in edited.validation_errors


# ---------------------------------------------------------------------------
# 4. The gate is not weakened
# ---------------------------------------------------------------------------


class TestTheGateStillRefuses:
    def test_genuinely_invalid_content_is_still_refused(self):
        """If this lane had met the gate by relaxing it, this passes."""
        result = RunbookValidator().validate_content(LEGACY_SHAPED_DRAFT)
        assert not result.passed
        assert "No YAML frontmatter found" in result.errors
        assert "Missing required section: Causes" in result.errors

    def test_the_no_llm_skeleton_is_v4_shaped(self):
        """Shaped like the thing the reviewer must produce, so the edit happens
        inside the schema instead of starting with a rewrite."""
        skeleton = SuggestionService.fallback_template("case-example")
        for section in (
            "## Symptom Recognition",
            "## Applicability",
            "## Diagnostic Steps",
            "## Causes",
            "## Prevention",
            "## Sources",
        ):
            assert section in skeleton
        assert "### Cause Z: Unidentified" in skeleton
        assert "[Default]" in skeleton

    def test_the_no_llm_skeleton_is_still_unpublishable(self):
        """v4-shaped must not mean approvable. A skeleton the gate accepted
        would let one click publish a blank form into the global corpus — a hole
        this lane would have opened, since the prose template it replaces was
        refused too."""
        result = RunbookValidator().validate_content(
            SuggestionService.fallback_template("case-example")
        )
        assert not result.passed
        assert "Cause A: **Statement:** sub-field is empty" in result.errors

    async def test_no_provider_yields_the_skeleton_and_says_so(self):
        suggestion = await _extract(_service(provider=None))

        assert "### Cause Z: Unidentified" in suggestion.suggested_content
        assert suggestion.validation_passed is False
        assert (
            "Cause A: **Statement:** sub-field is empty" in suggestion.validation_errors
        )

    async def test_a_truncated_draft_is_never_offered_as_a_runbook(self):
        """A runbook cut mid-procedure still carries frontmatter and the early
        headings, so it can PASS the gate while missing its last steps — the
        #1094 rule, applied here."""

        class TruncatingProvider:
            def __init__(self):
                self.calls = 0

            async def generate(self, **kwargs):
                self.calls += 1
                return SimpleNamespace(content=valid_runbook(), is_truncated=True)

        provider = TruncatingProvider()
        suggestion = await _extract(_service(provider))

        assert "### Cause Z: Unidentified" in suggestion.suggested_content
        assert suggestion.validation_passed is False


# ---------------------------------------------------------------------------
# 5. Redaction must not destroy the runbook (#1226 rework)
# ---------------------------------------------------------------------------


class TestRedactionKeepsTheDraftPublishable:
    """``_scan_for_pii`` used to scan ``f"{title}\n\n{content}"`` and assign the
    WHOLE sanitized buffer back to ``suggested_content``.

    A v4 runbook must open with ``---`` on line 1, so a title prepended in front
    of it guarantees that it never is. Measured over the eval's eight recorded
    drafts — all of which pass the gate — prepending a title flips all eight to
    ``['No YAML frontmatter found']``. With a real sanitizer, therefore, ANY
    suggestion carrying PII was permanently unapprovable, and the lane's
    headline was false in exactly the population it targets.
    """

    @staticmethod
    def _draft_with_pii() -> str:
        return valid_runbook().replace(
            "PostgreSQL 14+. Requires pg_monitor role. Tools: psql.",
            "PostgreSQL 14+ on prod-db-01. Requires pg_monitor role. Tools: psql.",
        )

    async def _extract_with_redaction(self, title: str = "Pool exhaustion"):
        sanitizer = RedactingSanitizer()
        svc = SuggestionService(
            case_repository=_case_repository(),
            knowledge_service=MagicMock(),
            sanitizer=sanitizer,
            llm_provider=ScriptedProvider(self._draft_with_pii()),
        )
        suggestion = await svc.extract_knowledge_from_case(
            case_id=CASE_ID,
            organization_id=ORG,
            extracted_by="user_extractor",
            title_suggestion=title,
        )
        return svc, sanitizer, suggestion

    async def test_the_frontmatter_survives_redaction(self):
        _svc, _san, suggestion = await self._extract_with_redaction()

        assert suggestion.suggested_content.startswith("---\n")
        assert RunbookValidator().validate_content(suggestion.suggested_content).passed

    async def test_the_recorded_verdict_still_passes(self):
        """The number the lane exists to move, measured on the branch that
        actually runs in cloud."""
        _svc, _san, suggestion = await self._extract_with_redaction()

        assert suggestion.validation_passed is True, suggestion.validation_errors
        assert suggestion.validation_errors == []

    async def test_pii_is_still_detected_and_removed(self):
        """Not fixed by disabling redaction — the redaction still happens."""
        _svc, _san, suggestion = await self._extract_with_redaction()

        assert suggestion.pii_scan_status is PIIScanStatus.PII_DETECTED
        assert "prod-db-01" not in suggestion.suggested_content
        assert "<HOST>" in suggestion.suggested_content

    async def test_the_title_is_redacted_too(self):
        """The other half of the same bug: the title was SCANNED but only
        content was written back, so PII in the title survived into
        ``upload_document(title=...)`` — the published item's name and, through
        ``runbook_filename``, its filename on disk."""
        _svc, _san, suggestion = await self._extract_with_redaction(
            title="Pool exhaustion on prod-db-01"
        )

        assert suggestion.suggested_title == "Pool exhaustion on <HOST>"
        assert "prod-db-01" not in suggestion.suggested_title

    async def test_title_and_content_are_scanned_as_separate_documents(self):
        """Never the concatenation — that is the thing that broke it."""
        _svc, sanitizer, suggestion = await self._extract_with_redaction()

        assert len(sanitizer.seen) == 2
        title_seen, content_seen = sanitizer.seen
        assert title_seen == "Pool exhaustion"
        assert content_seen.startswith("---\n")

    async def test_clean_content_is_not_rewritten(self):
        """A sanitizer that finds nothing must leave both fields alone and mark
        the scan CLEAN, exactly as before."""
        sanitizer = RedactingSanitizer(needle="nothing-here-to-find")
        svc = SuggestionService(
            case_repository=_case_repository(),
            knowledge_service=MagicMock(),
            sanitizer=sanitizer,
            llm_provider=ScriptedProvider(valid_runbook()),
        )

        suggestion = await _extract(svc)

        assert suggestion.pii_scan_status is PIIScanStatus.CLEAN
        assert _without_id_line(suggestion.suggested_content) == _without_id_line(
            valid_runbook()
        )
        assert suggestion.validation_passed is True


# ---------------------------------------------------------------------------
# 6. The recorded verdict is never stale (#1226 rework)
# ---------------------------------------------------------------------------


class TestTheVerdictTracksEveryContentMutation:
    """Three sites mutate content: extraction, the reviewer edit, and the
    SCAN_FAILED re-scan inside ``approve_suggestion``. The third one skipped
    ``_record_validation``, so a suggestion could carry
    ``validation_passed=True`` describing text that no longer existed. They are
    now one paired helper rather than two calls each site must remember."""

    class _BreakingOnRetry:
        """Fails the first scan, then redacts a required section heading — a
        redaction that itself breaks the gate, which is the case the pairing
        exists for."""

        def __init__(self):
            self.calls = 0

        async def asanitize(self, text: str) -> str:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("presidio unavailable")
            return text.replace("## Sources", "## Redacted")

    async def test_the_scan_failed_rescan_re_records_the_verdict(self):
        sanitizer = self._BreakingOnRetry()
        knowledge = MagicMock()
        knowledge.upload_document = AsyncMock(
            return_value={"document_id": "kb_abcdef0123456789"}
        )
        svc = SuggestionService(
            case_repository=_case_repository(),
            knowledge_service=knowledge,
            sanitizer=sanitizer,
            llm_provider=ScriptedProvider(valid_runbook()),
        )
        suggestion = await _extract(svc)
        assert suggestion.pii_scan_status is PIIScanStatus.SCAN_FAILED

        await svc.approve_suggestion(
            suggestion_id=suggestion.suggestion_id,
            reviewed_by="user-admin",
            organization_id=ORG,
        )

        # The re-scan removed a required section, so the verdict MUST have
        # moved with it. Left unpaired this stayed at whatever extraction last
        # recorded.
        assert suggestion.validation_passed is False
        assert "Missing required section: Sources" in suggestion.validation_errors

    def test_every_content_mutation_goes_through_the_paired_helper(self):
        """A structural pin, because the failure mode is forgetting. Any future
        site that calls the bare scan is a site that can leave a stale verdict
        behind."""
        import inspect

        from faultmaven.modules.knowledge.domain.services import suggestion_service

        source = inspect.getsource(suggestion_service.SuggestionService)
        bare = source.count("self._scan_for_pii(")
        paired = source.count("self._scan_and_record(")
        # Exactly one bare call: the one inside _scan_and_record itself.
        assert bare == 1, "a caller is scanning without re-recording the verdict"
        assert paired >= 3


# ---------------------------------------------------------------------------
# 7. The best draft, not merely the last (#1226 rework)
# ---------------------------------------------------------------------------


class TestTheBestDraftIsKept:
    async def test_a_worse_repair_turn_does_not_replace_a_near_miss(self):
        """A repair turn is not monotonic: it can "fix" three flagged errors by
        restructuring a section into two new ones. Overwriting unconditionally
        handed the reviewer that regression while the docstring promised them
        the best draft."""
        near_miss = valid_runbook().replace("## Sources", "## Not Sources")
        much_worse = "# Not a runbook at all\n\nJust prose.\n"
        provider = ScriptedProvider(near_miss, much_worse)

        suggestion = await _extract(_service(provider))

        assert len(provider.prompts) == 2
        assert _without_id_line(suggestion.suggested_content) == _without_id_line(
            near_miss
        )
        assert suggestion.validation_errors == ["Missing required section: Sources"]

    async def test_a_better_repair_turn_does_replace_the_first(self):
        """The complement — otherwise "keep the best" could be implemented as
        "keep the first" and pass the test above."""
        much_worse = "# Not a runbook at all\n\nJust prose.\n"
        near_miss = valid_runbook().replace("## Sources", "## Not Sources")
        provider = ScriptedProvider(much_worse, near_miss)

        suggestion = await _extract(_service(provider))

        assert _without_id_line(suggestion.suggested_content) == _without_id_line(
            near_miss
        )
        assert suggestion.validation_errors == ["Missing required section: Sources"]


# ---------------------------------------------------------------------------
# 8. Filling one field must not publish the form (#1226 rework)
# ---------------------------------------------------------------------------


class TestTheSkeletonNeedsRealAuthoring:
    _FILLS = {
        "Statement": (
            "**Statement:**\n",
            "**Statement:** The single root cause.\n",
        ),
        "Indicators": (
            "**Indicators:**\n",
            "**Indicators:**\n- root: [Step 1] the observable\n",
        ),
        "Interventions": (
            "**Interventions:**\n",
            "**Interventions:**\n- **remediation** (root): the fix.\n"
            "  **Verification:** re-run Step 1.\n",
        ),
    }

    def test_all_three_cause_subfields_start_empty(self):
        result = RunbookValidator().validate_content(
            SuggestionService.fallback_template("case-example")
        )
        assert not result.passed
        for sub in ("Statement", "Indicators", "Interventions"):
            assert f"Cause A: **{sub}:** sub-field is empty" in result.errors

    @pytest.mark.parametrize("field", ["Statement", "Indicators", "Interventions"])
    def test_filling_any_single_field_still_does_not_publish(self, field):
        """The regression this replaces: with only the Statement empty, ONE
        keystroke cleared the single remaining error while every other field —
        the frontmatter ``title`` included — still read
        ``[INSUFFICIENT SOURCE DATA]``."""
        old, new = self._FILLS[field]
        content = SuggestionService.fallback_template("case-example").replace(
            old, new, 1
        )

        assert not RunbookValidator().validate_content(content).passed

    def test_authoring_the_whole_cause_does_clear_the_gate(self):
        """The complement: the block is three real authoring acts away from
        valid, not unreachable. Without this the class above would also pass if
        the skeleton were simply broken beyond repair."""
        content = SuggestionService.fallback_template("case-example")
        for old, new in self._FILLS.values():
            content = content.replace(old, new, 1)

        assert RunbookValidator().validate_content(content).passed

    def test_the_hint_comment_declares_no_subfield_labels(self):
        """``parse_cause_subfields`` does NOT strip HTML comments, so a
        ``**Statement:**`` example written inside the guidance comment is read
        as the real field — measured: it made the empty skeleton PASS. The
        comment must therefore never spell a sub-field label."""
        skeleton = SuggestionService.fallback_template("case-example")
        comment = skeleton[skeleton.index("<!--") : skeleton.index("-->")]
        for sub in ("Statement", "Indicators", "Interventions", "Chain"):
            assert f"**{sub}:**" not in comment
