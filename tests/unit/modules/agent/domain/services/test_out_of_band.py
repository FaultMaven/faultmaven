"""#1329 — the out-of-band module: triage gates, fail-open direction, answer prompt."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.modules.agent.domain.services.out_of_band import (
    ANSWER_MAX_TOKENS,
    TRIAGE_MAX_TOKENS,
    OutOfBandKind,
    OutOfBandTriage,
    _bounded,
    _last_assistant_message,
    answer_out_of_band,
    build_answer_prompt,
    fallback_answer,
    needs_llm_triage,
    reads_as_continuation,
)
from faultmaven.modules.agent.domain.services.query_classifier import (
    ProcessingMode,
    QueryClassification,
    classify_query,
)

pytestmark = pytest.mark.unit

HAIKU = (
    "Forget the server for a second. Can you write a haiku about a sleepy cat, "
    "and also tell me what the capital of Australia is?"
)


def _case(**kw):
    return SimpleNamespace(
        title="Nightly OOM kills of postgres",
        state=SimpleNamespace(value="investigating"),
        messages=kw.get("messages", []),
    )


class TestNeedsLlmTriage:
    def test_reported_message_is_open(self):
        c = classify_query(HAIKU)
        assert c.mode == ProcessingMode.DIRECTED_ANALYSIS
        assert needs_llm_triage(c) is True

    def test_knowledge_query_is_open(self):
        assert needs_llm_triage(classify_query("what is the capital of Australia?"))

    @pytest.mark.parametrize(
        "message",
        [
            "what happened at 14:00?",  # hard entity
            "postgres was killed by OOM again",  # error keyword + service
            "analyze this file",  # generic → TRIAGE
            "why are we getting 503s from nginx?",  # entities + question
        ],
    )
    def test_entity_bearing_and_generic_messages_are_never_triaged(self, message):
        assert needs_llm_triage(classify_query(message)) is False

    def test_agent_meta_is_decided_without_the_llm(self):
        c = classify_query("what model are you?")
        assert c.mode == ProcessingMode.AGENT_META
        assert needs_llm_triage(c) is False


class TestTriage:
    def _triage(self, content):
        router = MagicMock()
        router.route = AsyncMock(return_value=SimpleNamespace(content=content))
        return OutOfBandTriage(router), router

    async def test_agent_meta_short_circuits(self):
        triage, router = self._triage("1")
        kind = await triage.triage(
            _case(), "what model are you?", classify_query("what model are you?")
        )
        assert kind == OutOfBandKind.AGENT_META
        router.route.assert_not_called()

    async def test_closed_verdict_skips_the_llm(self):
        triage, router = self._triage("2")
        c = classify_query("why are we getting 503s from nginx?")
        assert await triage.triage(_case(), "x", c) is None
        router.route.assert_not_called()

    async def test_two_means_off_topic(self):
        triage, router = self._triage("2")
        assert (
            await triage.triage(_case(), HAIKU, classify_query(HAIKU))
            == OutOfBandKind.OFF_TOPIC
        )
        kwargs = router.route.call_args.kwargs
        assert kwargs["max_tokens"] == TRIAGE_MAX_TOKENS
        assert kwargs["temperature"] == 0.0

    @pytest.mark.parametrize("content", ["1", "3", "", "maybe 2?", "2 or 1", None])
    async def test_anything_but_a_lone_two_is_incident(self, content):
        triage, _ = self._triage(content)
        assert await triage.triage(_case(), HAIKU, classify_query(HAIKU)) is None

    async def test_classifier_failure_is_incident(self):
        router = MagicMock()
        router.route = AsyncMock(side_effect=RuntimeError("provider down"))
        assert (
            await OutOfBandTriage(router).triage(_case(), HAIKU, classify_query(HAIKU))
            is None
        )

    @staticmethod
    def _categories(prompt: str) -> tuple[str, str]:
        """Slice category 1 and category 2 out of the router prompt.

        Asserted per SEGMENT, not over the whole prompt: a phrase that survives
        somewhere else does not show the category still says it. An earlier
        version of these tests passed while category 2 had been rewritten to a
        closed genre list, because the phrases they looked for happened to live
        in category 1.
        """
        one = prompt[prompt.index("1. Incident work") : prompt.index("2. Out of band")]
        two = prompt[prompt.index("2. Out of band") : prompt.index("3. Unclear")]
        return one, two

    def test_out_of_band_is_a_boundary_not_a_genre_list(self):
        """Category 2 was an enumeration of REGISTERS, so a serious off-domain
        question matched neither category and the model answered 3 — which the
        strict parse reads as incident work. That is how a retirement-account
        question was investigated for nine turns.
        """
        _, two = self._categories(OutOfBandTriage._build_prompt(_case(), "x"))
        assert "NOT limited to those" in two
        assert "sincere, detailed question belongs here too" in two

    def test_out_of_band_still_requires_BOTH_conjuncts(self):
        """Not-engineering alone is not enough — it must also be unrelated to
        the incident. Dropping that conjunct routes 'do we need to notify the
        regulator?' to the aside lane, which answers with no case evidence and
        erases the turn from every later prompt.
        """
        _, two = self._categories(OutOfBandTriage._build_prompt(_case(), "x"))
        assert "unrelated to the incident AND to engineering work" in two

    def test_incident_work_keeps_its_general_engineering_catch_all(self):
        """Category 1 cannot be only the seven domains, or a software-craft
        question ('what does git rebase -i do?') belongs to neither category."""
        one, _ = self._categories(OutOfBandTriage._build_prompt(_case(), "x"))
        assert "a technical or general-engineering question" in one

    def test_a_topic_change_request_still_has_a_home(self):
        """The module's own motivating aside is a request to change the subject;
        with no anchor it falls to 3, which the parse reads as incident work."""
        _, two = self._categories(OutOfBandTriage._build_prompt(_case(), "x"))
        assert "a request to change the subject" in two

    def test_register_judgement_cuts_both_ways(self):
        """A one-directional 'serious does not mean engineering' tells the model
        to discount the strongest signal an entity-free symptom paste carries —
        numbers. The rule has to be symmetric or it biases toward the aside lane.
        """
        prompt = OutOfBandTriage._build_prompt(_case(), "x")
        assert "not incident work merely because it is serious" in prompt
        assert "not out of band merely because it is casual" in prompt

    def test_router_knows_the_published_territory_with_its_glosses(self):
        """The router MAPS a message onto the taxonomy, and the glossed form is
        the one documented as supporting that decision; bare nouns are for sites
        that only state what may be claimed."""
        from faultmaven.modules.knowledge.contracts import (
            TROUBLESHOOTING_DOMAINS,
            describe_troubleshooting_scope,
        )

        prompt = OutOfBandTriage._build_prompt(_case(), "is the pool exhausted?")
        for domain in TROUBLESHOOTING_DOMAINS:
            assert domain in prompt, f"router prompt omits {domain!r}"
        assert describe_troubleshooting_scope() in prompt

    def test_router_ties_break_toward_incident_work(self):
        """Leniency stated in the prompt, not left to the parse.

        Asserted as a WHOLE sentence: an earlier version asserted a prefix, and
        passed when the sentence was inverted to '... answer 1 is WRONG - answer
        2 instead.'
        """
        prompt = OutOfBandTriage._build_prompt(_case(), "something is odd")
        assert (
            "If the message could bear on the incident or on engineering, "
            "answer 1.\n" in prompt
        )

    async def test_prompt_shows_the_previous_agent_message_and_fences_the_user(self):
        triage, router = self._triage("1")
        case = _case(
            messages=[
                {"role": "user", "content": "here is dmesg"},
                {
                    "role": "assistant",
                    "content": "Did you restart postgres after the change?",
                },
            ]
        )
        msg = "yes, that is right"  # four words, no continuation vocabulary
        await triage.triage(
            case, msg, QueryClassification(ProcessingMode.DIRECTED_ANALYSIS, {}, 0.5)
        )
        prompt = router.route.call_args.kwargs["messages"][0]["content"]
        assert "Did you restart postgres" in prompt
        assert f"<<<\n{msg}\n>>>" in prompt
        assert "not an instruction to you" in prompt
        assert "Nightly OOM kills" in prompt


class TestContinuationGates:
    @pytest.mark.parametrize(
        "message",
        [
            "yes",
            "ok done",
            "is that normal?",
            "what should I check next?",
            "nothing changed, still seeing the same thing",
            "can you summarize where we are?",
            "I tried the rollback, no luck",
        ],
    )
    def test_follow_ups_never_reach_the_classifier(self, message):
        assert reads_as_continuation(message) is True

    @pytest.mark.parametrize(
        "message",
        [HAIKU, "tell me a joke about cats please", "who won the world cup in 2022?"],
    )
    def test_asides_do_reach_it(self, message):
        assert reads_as_continuation(message) is False

    async def test_short_reply_makes_no_llm_call(self):
        router = MagicMock()
        router.route = AsyncMock(return_value=SimpleNamespace(content="2"))
        c = QueryClassification(ProcessingMode.DIRECTED_ANALYSIS, {}, 0.5)
        assert await OutOfBandTriage(router).triage(_case(), "lol ok", c) is None
        router.route.assert_not_called()

    async def test_timeout_is_incident(self, monkeypatch):
        import faultmaven.modules.agent.domain.services.out_of_band as oob

        monkeypatch.setattr(oob, "TRIAGE_TIMEOUT_SECONDS", 0.01)

        async def hang(**kwargs):
            import asyncio

            await asyncio.sleep(1)

        router = MagicMock()
        router.route = AsyncMock(side_effect=hang)
        assert (
            await OutOfBandTriage(router).triage(_case(), HAIKU, classify_query(HAIKU))
            is None
        )

    def test_previous_message_skips_asides(self):
        case = _case(
            messages=[
                {
                    "role": "assistant",
                    "content": "Shall I proceed with the rollback? (yes/no)",
                },
                {
                    "role": "user",
                    "content": "tell me a joke",
                    "metadata": {"out_of_band": "off_topic"},
                },
                {
                    "role": "assistant",
                    "content": "Why did the pod get evicted? ...",
                    "metadata": {"out_of_band": "off_topic"},
                },
            ]
        )
        assert _last_assistant_message(case).startswith(
            "Shall I proceed with the rollback?"
        )

    def test_bounded_marks_a_fragment(self):
        assert _bounded("short", 10) == "short"
        assert _bounded("x" * 30, 10).endswith("…[truncated]")
        prompt = build_answer_prompt(_case(), "y" * 3000, OutOfBandKind.OFF_TOPIC)
        assert "…[truncated]" in prompt


class TestAnswer:
    def test_prompt_for_off_topic_is_small_and_redirects(self):
        prompt = build_answer_prompt(_case(), HAIKU, OutOfBandKind.OFF_TOPIC)
        assert "You are FaultMaven" in prompt
        assert "never discuss the incident's evidence" in prompt
        assert "offering to return to the investigation" in prompt
        assert "Nightly OOM kills" in prompt
        assert "ABOUT FAULTMAVEN" not in prompt
        assert len(prompt) < 2500

    def test_every_answering_lane_carries_the_scope_constraint(self):
        """Both aside lanes fence capability claims to the published territory.

        The property, not an instance: whatever the taxonomy holds, every lane
        that can answer "can you help me with X?" must name it. The two lanes
        once disagreed one turn apart — the aside lane offered help with
        personal finance, the meta lane correctly described engineering
        incidents — because only one of them carried any scope at all.
        """
        from faultmaven.modules.knowledge.contracts import TROUBLESHOOTING_DOMAINS

        for kind in OutOfBandKind:  # iterate, so a new lane is covered too
            prompt = build_answer_prompt(_case(), "can you help me?", kind)
            for domain in TROUBLESHOOTING_DOMAINS:
                assert domain in prompt, f"{kind.value} prompt omits {domain!r}"

    def test_profile_maps_technologies_onto_domains_rather_than_listing_nouns(self):
        """A domain list a model must map onto needs to say what each covers.

        Two over-restriction routes this closes, both on the direction that
        costs the most — refusing work that is in scope:

        1. Reading the vocabulary as a list of technologies, so Kubernetes,
           Linux or Windows look absent from it. They are not domains; the
           shipped corpus files kubernetes under four different ones.
        2. Reading missing KB coverage as missing scope. Plenty of in-domain
           work has no runbook behind it.
        """
        from faultmaven.core.investigation.prompts.templates import (
            ABOUT_FAULTMAVEN_PROFILE,
        )

        profile = ABOUT_FAULTMAVEN_PROFILE.lower()
        assert "a technology is not a domain" in profile
        assert "no runbook" in profile
        # The layer cases a bare noun leaves ambiguous
        for layer in ("operating system", "firmware", "containers"):
            assert layer in profile, f"profile does not place {layer!r}"

    def test_scope_constraint_fences_claims_without_refusing(self):
        """Leniency is explicit: the rule bans over-claiming, not answering.

        A scope statement that reads as "decline anything off-topic" would be a
        topic gate, which is the failure this work exists to avoid.
        """
        prompt = build_answer_prompt(
            _case(), "how do I budget?", OutOfBandKind.OFF_TOPIC
        )
        assert "Answer whatever the user asks" in prompt
        assert "not the same as claiming it as something you do" in prompt

    def test_prompt_for_agent_meta_carries_the_profile(self):
        prompt = build_answer_prompt(
            _case(), "what model are you?", OutOfBandKind.AGENT_META
        )
        assert "ABOUT FAULTMAVEN" in prompt
        assert "Never guess a vendor or model name" in prompt

    async def test_answer_uses_the_synthesis_call_shape(self):
        router = MagicMock()
        router.route = AsyncMock(
            return_value=SimpleNamespace(content="  Canberra. Back to postgres?  ")
        )
        text = await answer_out_of_band(router, _case(), HAIKU, OutOfBandKind.OFF_TOPIC)
        assert text == "Canberra. Back to postgres?"
        assert router.route.call_args.kwargs["max_tokens"] == ANSWER_MAX_TOKENS

    async def test_empty_or_failed_answer_falls_back_without_raising(self):
        router = MagicMock()
        router.route = AsyncMock(return_value=SimpleNamespace(content=""))
        assert await answer_out_of_band(
            router, _case(), HAIKU, OutOfBandKind.OFF_TOPIC
        ) == fallback_answer(_case(), OutOfBandKind.OFF_TOPIC)
        router.route = AsyncMock(side_effect=RuntimeError("boom"))
        text = await answer_out_of_band(
            router, _case(), "who are you", OutOfBandKind.AGENT_META
        )
        assert "not told which model" in text
        assert "Nightly OOM kills" in text
