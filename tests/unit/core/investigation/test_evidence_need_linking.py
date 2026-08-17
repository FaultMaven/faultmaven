"""Every EVIDENCE ask gets a durable identity in the needs pool (#1079).

The defect these pin: an EVIDENCE-type ``SuggestedFollowUp`` could be emitted
with no ``evidence_need_id``, and nothing noticed. Across 19 recorded simulator
runs (six scenarios, 138 EVIDENCE suggestions) the field was populated zero
times, so both anti-nagging mechanisms — the obtainability wall and mention
decay, which act on an ``EvidenceNeed`` — had nothing to act on. On the aws-iam
scenario the agent asked for the same target-account record across ten
consecutive turns while the user declined six times.

The existing ``test_evidence_need_id_resolution.py`` covers the seam that
resolves the field once it is set; nothing covered the field never being set.
"""

import pytest

from faultmaven.core.investigation.evidence_need_linking import (
    _MAX_BACKFILLED_PER_TURN,
    _similarity,
    link_evidence_suggestions_to_needs,
)
from faultmaven.modules.case.contracts import (
    EvidenceNeed,
    NeedObtainability,
    NeedPriority,
    NeedPurpose,
    NeedState,
)

CASE_ID = "case_000000000001"


class _FollowUp:
    """Stand-in for ``SuggestedFollowUp`` — the linker only touches these
    attributes, and the real model's validators reject a mid-flight mutation
    pattern the engine relies on."""

    def __init__(self, action_type="EVIDENCE", body="", label="", need_id=None):
        self.action_type = action_type
        self.body = body
        self.label = label
        self.evidence_need_id = need_id


class _Case:
    def __init__(self, needs=None, hypotheses=None):
        self.case_id = CASE_ID
        self.evidence_needs = list(needs or [])
        self.hypotheses = hypotheses or {}


def _resolve(ref, created, _prefix):
    """Stand-in for ``MilestoneEngine._resolve_id_ref``."""
    if isinstance(ref, str) and ref.startswith("new_index_"):
        idx = int(ref.rsplit("_", 1)[1])
        return created[idx] if idx < len(created) else ref
    return ref


def _need(text, *, state=NeedState.PENDING, purpose=NeedPurpose.CAUSAL_VERIFICATION):
    return EvidenceNeed(
        case_id=CASE_ID,
        purpose=purpose,
        request_text=text,
        rationale="because",
        priority=NeedPriority.MEDIUM,
        state=state,
        superseded_reason="gone" if state == NeedState.SUPERSEDED else None,
        fulfilling_evidence_ids=["ev_1"] if state == NeedState.FULFILLED else [],
        created_at_turn=1,
    )


def _link(case, follow_ups, turn=5, metadata=None):
    meta = metadata if metadata is not None else {}
    link_evidence_suggestions_to_needs(case, follow_ups, meta, turn, _resolve)
    return meta


# ============================================================
# The core guarantee: no EVIDENCE ask leaves without a need
# ============================================================


@pytest.mark.unit
class TestEveryEvidenceAskGetsANeed:
    def test_unlinked_ask_creates_a_need_and_links_it(self):
        case = _Case()
        fu = _FollowUp(body="Provide the target-account OIDC provider record")

        _link(case, [fu])

        assert len(case.evidence_needs) == 1
        assert fu.evidence_need_id == case.evidence_needs[0].need_id

    def test_created_need_is_reported_in_metadata(self):
        """``evidence_needs_updated`` is what ``_flatten_follow_ups`` resolves
        ``new_index_N`` against; a need created here must appear in it."""
        case = _Case()
        meta = _link(case, [_FollowUp(body="Provide the provider record")])

        assert meta["evidence_needs_updated"] == [case.evidence_needs[0].need_id]

    def test_ask_is_recorded_on_the_needs_history(self):
        case = _Case()
        _link(case, [_FollowUp(body="Provide the provider record")], turn=7)

        need = case.evidence_needs[0]
        assert need.surfaced_turns == [7]
        assert need.times_surfaced == 1
        assert need.last_surfaced_turn == 7

    def test_non_evidence_suggestions_are_untouched(self):
        case = _Case()
        run = _FollowUp(action_type="RUN", body="aws iam get-role ...")
        speech = _FollowUp(action_type="FREE_SPEECH", body="Ask about the fix")

        _link(case, [run, speech])

        assert case.evidence_needs == []
        assert run.evidence_need_id is None
        assert speech.evidence_need_id is None

    def test_ask_with_no_text_is_skipped(self):
        """An empty ask cannot be matched or described, and ``request_text``
        rejects whitespace-only — creating one would raise mid-turn."""
        case = _Case()
        _link(case, [_FollowUp(body="   ", label="")])

        assert case.evidence_needs == []

    def test_label_is_used_when_body_is_absent(self):
        case = _Case()
        _link(case, [_FollowUp(body="", label="Share the CloudTrail event")])

        assert case.evidence_needs[0].request_text == "Share the CloudTrail event"


# ============================================================
# The loop shape: a re-ask must be a second mention, not a second need
# ============================================================


@pytest.mark.unit
class TestRepeatedAskIncrementsRatherThanDuplicates:
    def test_same_ask_next_turn_matches_the_existing_need(self):
        case = _Case()
        _link(case, [_FollowUp(body="Provide the target-account OIDC provider record")])
        _link(
            case,
            [_FollowUp(body="Provide the target-account OIDC provider record")],
            turn=6,
        )

        assert len(case.evidence_needs) == 1
        assert case.evidence_needs[0].surfaced_turns == [5, 6]

    def test_reworded_repeat_still_matches(self):
        """The fm#1079 asks were reworded every turn ("Share target-account IAM
        output" / "Share the target-account provider record"). Order-sensitive
        matching would score each as new and the count would never rise."""
        case = _Case([_need("Provide the target-account OIDC provider record")])

        _link(
            case,
            [
                _FollowUp(
                    body="Have the analytics team return the OIDC provider "
                    "record for the target account"
                )
            ],
        )

        assert len(case.evidence_needs) == 1
        assert case.evidence_needs[0].times_surfaced == 1

    def test_ten_turn_loop_reads_as_ten_asks_on_one_need(self):
        """The exact fm#1079 shape, compressed: the same ask on ten
        consecutive turns must leave one need reporting ten asks — the signal
        both anti-nagging mechanisms need and never received."""
        case = _Case()
        for turn in range(6, 16):
            _link(
                case,
                [_FollowUp(body="Provide the target-account OIDC provider record")],
                turn=turn,
            )

        assert len(case.evidence_needs) == 1
        assert case.evidence_needs[0].times_surfaced == 10
        assert case.evidence_needs[0].last_surfaced_turn == 15

    def test_two_asks_for_the_same_need_in_one_turn_count_once(self):
        """The count means "turns on which I asked" — that is the quantity the
        decay rule reasons about."""
        case = _Case()
        _link(
            case,
            [
                _FollowUp(body="Provide the OIDC provider record"),
                _FollowUp(body="Provide the OIDC provider record"),
            ],
            turn=4,
        )

        assert len(case.evidence_needs) == 1
        assert case.evidence_needs[0].surfaced_turns == [4]

    def test_a_genuinely_different_ask_creates_its_own_need(self):
        case = _Case([_need("Provide the target-account OIDC provider record")])

        _link(case, [_FollowUp(body="Share the pod logs from the failing deployment")])

        assert len(case.evidence_needs) == 2


# ============================================================
# Matching only considers live asks
# ============================================================


@pytest.mark.unit
class TestMatchCandidates:
    @pytest.mark.parametrize("terminal", [NeedState.FULFILLED, NeedState.SUPERSEDED])
    def test_terminal_needs_are_not_match_candidates(self, terminal):
        """A fulfilled or superseded need is not an outstanding ask; folding a
        fresh request into one would hide it from the surfacing path entirely."""
        case = _Case([_need("Provide the OIDC provider record", state=terminal)])

        _link(case, [_FollowUp(body="Provide the OIDC provider record")])

        assert len(case.evidence_needs) == 2
        assert case.evidence_needs[1].state == NeedState.PENDING

    def test_partially_met_needs_are_match_candidates(self):
        case = _Case(
            [
                _need(
                    "Provide the OIDC provider record",
                    state=NeedState.PARTIALLY_MET,
                )
            ]
        )

        _link(case, [_FollowUp(body="Provide the OIDC provider record")])

        assert len(case.evidence_needs) == 1


# ============================================================
# The model-declared path stays authoritative
# ============================================================


@pytest.mark.unit
class TestDeclaredLinkWins:
    def test_declared_real_id_is_honoured_and_recorded(self):
        existing = _need("Provide the OIDC provider record")
        case = _Case([existing, _need("Share the pod logs")])
        fu = _FollowUp(body="something else entirely", need_id=existing.need_id)

        _link(case, [fu], turn=9)

        assert len(case.evidence_needs) == 2
        assert fu.evidence_need_id == existing.need_id
        assert existing.surfaced_turns == [9]

    def test_declared_new_index_resolves_against_this_turns_needs(self):
        existing = _need("Provide the OIDC provider record")
        case = _Case([existing])
        fu = _FollowUp(body="unrelated text", need_id="new_index_0")

        _link(
            case,
            [fu],
            metadata={"evidence_needs_updated": [existing.need_id]},
        )

        assert fu.evidence_need_id == existing.need_id
        assert len(case.evidence_needs) == 1

    def test_declared_id_that_resolves_to_nothing_falls_back_to_matching(self):
        """Graceful degradation — a dangling ref must not cost the ask its
        identity, which is the whole point of the module."""
        existing = _need("Provide the OIDC provider record")
        case = _Case([existing])
        fu = _FollowUp(
            body="Provide the OIDC provider record", need_id="eneed_deadbeef"
        )

        _link(case, [fu])

        assert fu.evidence_need_id == existing.need_id
        assert existing.times_surfaced == 1


# ============================================================
# Inferred purpose + the sweep interaction
# ============================================================


@pytest.mark.unit
class TestInferredNeedShape:
    def test_ask_before_any_hypothesis_is_symptom_shaped(self):
        case = _Case(hypotheses={})
        _link(case, [_FollowUp(body="Share the pod logs")])

        assert case.evidence_needs[0].purpose == NeedPurpose.SYMPTOM_VERIFICATION

    def test_ask_with_a_differential_open_is_causal(self):
        case = _Case(hypotheses={"hyp_1": object()})
        _link(case, [_FollowUp(body="Share the pod logs")])

        assert case.evidence_needs[0].purpose == NeedPurpose.CAUSAL_VERIFICATION

    def test_backfilled_need_carries_no_motivating_hypothesis(self):
        """Deliberate: the engine knows the ask was made, not which candidate
        it separates. Inventing a motivator would let the terminal-hypothesis
        sweep supersede an ask the user is still being shown."""
        case = _Case(hypotheses={"hyp_1": object()})
        _link(case, [_FollowUp(body="Share the pod logs")])

        assert case.evidence_needs[0].motivating_hypothesis_ids == []

    def test_backfilled_need_starts_obtainability_unknown(self):
        """UNKNOWN is the fail-safe: it keeps the case engaging until the model
        declares a wall, and never contributes one on its own."""
        case = _Case()
        _link(case, [_FollowUp(body="Share the pod logs")])

        assert case.evidence_needs[0].obtainability == NeedObtainability.UNKNOWN

    def test_overlong_ask_is_truncated_to_the_column_bound(self):
        case = _Case()
        _link(case, [_FollowUp(body="x " * 600)])

        assert len(case.evidence_needs[0].request_text) <= 500


#: Mutually disjoint asks — no shared content words, so each is genuinely new
#: and the matcher cannot fold them together.
_DISTINCT_ASKS = [
    "kubectl pod restart counts",
    "postgres slow query log",
    "nginx upstream latency histogram",
    "kafka consumer lag metrics",
    "redis eviction statistics",
    "systemd journal boot errors",
    "terraform plan diff output",
]


@pytest.mark.unit
class TestCreationCap:
    def test_creation_is_capped_per_turn(self):
        case = _Case()
        asks = [_FollowUp(body=a) for a in _DISTINCT_ASKS]
        assert len(asks) > _MAX_BACKFILLED_PER_TURN

        _link(case, asks)

        assert len(case.evidence_needs) == _MAX_BACKFILLED_PER_TURN

    def test_cap_does_not_block_matching_existing_needs(self):
        """The cap bounds pool growth, not the ask history — a repeat must
        still count even on a turn that hit the cap."""
        existing = _need("Provide the target-account OIDC provider record")
        case = _Case([existing])
        asks = [_FollowUp(body=a) for a in _DISTINCT_ASKS]
        asks.append(_FollowUp(body="Provide the target-account OIDC provider record"))

        _link(case, asks, turn=8)

        assert existing.surfaced_turns == [8]


@pytest.mark.unit
class TestSimilarity:
    def test_identical_text_scores_one(self):
        assert _similarity("the provider record", "the provider record") == 1.0

    def test_disjoint_text_scores_zero(self):
        assert _similarity("pod restart counts", "billing invoice totals") == 0.0

    def test_stopword_only_overlap_does_not_match(self):
        """Nearly every ask says "please provide the ... output"; if those words
        counted, two unrelated requests would fold together."""
        assert (
            _similarity("please provide the output", "please provide the data") == 0.0
        )

    def test_empty_text_scores_zero(self):
        assert _similarity("", "the provider record") == 0.0


# ============================================================
# Engine wiring — the ordering is load-bearing
# ============================================================


@pytest.mark.unit
class TestEngineWiring:
    """Static pins in the style of ``TestBothCallSitesUseFlattener``.

    Linking has to happen at a specific point in the turn: after the terminal
    sweep (so a need superseded this turn is not a match candidate), before
    ``repository.save`` (or created needs and the recorded ask evaporate), and
    before ``_flatten_follow_ups`` (or the wire response ships the nulls this
    whole change exists to stop). A reorder breaks the fix while every
    unit test above still passes, so the order is pinned here.
    """

    def _source(self):
        import inspect

        from faultmaven.core.investigation.milestone_engine import MilestoneEngine

        return inspect.getsource(MilestoneEngine._process_turn_impl)

    def test_turn_path_calls_the_linker(self):
        assert "link_evidence_suggestions_to_needs(" in self._source(), (
            "_process_turn_impl no longer links EVIDENCE suggestions to needs "
            "— every ask goes back to being a free-floating string that "
            "neither the obtainability wall nor mention decay can see (#1079)."
        )

    def test_linking_runs_before_the_save(self):
        src = self._source()
        assert src.index("link_evidence_suggestions_to_needs(") < src.index(
            "await self.repository.save(case_updated)"
        ), "linking must precede save() or created needs and the ask history are lost"

    def test_linking_runs_before_flattening(self):
        src = self._source()
        assert src.index("link_evidence_suggestions_to_needs(") < src.index(
            "follow_ups = self._flatten_follow_ups("
        ), "linking must precede flattening or the wire response carries nulls"

    def test_linking_runs_after_the_terminal_sweep(self):
        src = self._source()
        assert src.index(
            "_sweep_needs_for_terminal_hypotheses(case_updated)"
        ) < src.index(
            "link_evidence_suggestions_to_needs("
        ), "a need superseded this turn must not be a match candidate"


@pytest.mark.unit
class TestWireResponseCarriesTheNeedId:
    """The observable the simulator measured: across 19 runs and 138 EVIDENCE
    suggestions, the ``evidence_need_id`` on the persisted message row was
    ``null`` every single time. Linking then flattening must put a real
    ``eneed_`` ID on the wire."""

    def test_flattened_suggestion_carries_the_id(self):
        from types import SimpleNamespace

        from faultmaven.core.investigation.milestone_engine import MilestoneEngine

        engine = MilestoneEngine.__new__(MilestoneEngine)
        case = _Case()
        fu = SimpleNamespace(
            label="Share target provider details",
            action_type="EVIDENCE",
            payload=None,
            body="Provide the target-account OIDC provider record",
            hints=None,
            evidence_need_id=None,
        )

        meta = {}
        link_evidence_suggestions_to_needs(
            case, [fu], meta, 7, MilestoneEngine._resolve_id_ref.__get__(engine)
        )
        out = engine._flatten_follow_ups([fu], meta)

        assert out[0]["evidence_need_id"] == case.evidence_needs[0].need_id
        assert out[0]["evidence_need_id"].startswith("eneed_")
