"""Per-turn case telemetry — the observe-only stream that makes a stalled case
attributable to a side (#1142).

The question this stream exists to answer is **"did the engine alone stall this
case?"**, and the reason it needs its own channel is that nothing already
recorded can answer it:

* ``turns_without_progress`` is a NOR over the arms of
  ``MilestoneEngine._check_if_progress_made``, and those arms straddle both
  parties — user data (``novel_files_uploaded``) sits beside engine output
  (hypotheses, solutions, milestones, links). One live arm on either side holds
  the counter at 0, so it fires only on a JOINT stall and cannot isolate the
  engine.
* Four of that predicate's nine arms — ``novel_evidence_added``,
  ``novel_solutions_proposed``, ``novel_files_uploaded``,
  ``status_transitioned`` and ``hypothesis_evidence_links_applied`` — live only
  on the engine's in-flight working dict and are **written nowhere**. The stored
  ``TurnProgress`` keeps the RAW artifact lists instead, so
  ``progress_made=True`` with every stored list empty is both the normal shape
  of a legitimate upload turn and the exact shape of a lying counter. Measured
  on the local corpus at the time of writing: 376 of 1018 progress turns (36.9%)
  are bare in that way, which is why the decision has to be recorded where it is
  MADE rather than reconstructed afterwards.
* ``grounding_assessment`` (``milestone_engine._log_grounding_assessment``) is
  the right SHAPE but unusable as a stall signal: it is ``logger.debug`` behind
  an ``isEnabledFor`` guard (pods run at INFO), it is emitted from
  ``_recompute_assessment_state`` — inside response application, BEFORE the
  progress decision and the counter update — so its ``turns_without_progress``
  is last turn's, and it fires only on the generation path. It stays as-is: it
  is a grounding/seam trace, not a progress ledger, and this module does not
  replace it.

Design constraints carried over from the issue, and how they are met here:

**Observe-only, within a case.** Nothing in the engine reads this stream, and it
is never referenced in a prompt. An "idle" flag fed back into the running case
would recreate the nagging failure of #1138 with extra steps, and prompt text
naming a metric invites the model to satisfy the metric — minting a spurious
hypothesis to clear a flag is easier than investigating. Aggregation across
cases, offline, is the intended consumer.

**One event per consumed turn, on every path.** The single emission point is the
service chokepoint (``InvestigationService.process_turn``), which is where
``case.current_turn`` is advanced. That is deliberate and is not the same as the
"emit at the end of the engine turn" the issue first proposed: several paths
consume a turn number without ever reaching ``MilestoneEngine.process_turn`` at
all (greeting, file reclassification), and the terminal Q&A path returns from
the engine before any of its turn bookkeeping runs. Emitting per engine path
would leave those turns as GAPS, and a gap does not read as "nothing happened" —
it silently shortens every streak computed over the stream, so a correct
multi-turn handshake reads as an engine-dry run. One event at the point the turn
number is consumed is structurally immune to a path added later.

**Gameability, and its counterweight.** ``engine_advanced`` is gameable by
construction — its arms are LLM-authored artifacts, so minting one spurious
hypothesis per turn makes the engine permanently "not idle", and a stream
carrying only that bit would CERTIFY a spinning engine as healthy. The frontier
fields (hypothesis/causal-node state histograms, the outstanding-need pool, the
undisposed-input pool) ship in the SAME event for that reason: non-convergence
is computable by any consumer from the stream alone, with no engine-side rule
and nothing for the model to optimise against.

**Content-free, enforced.** Every value is a count, an id, an enum or a bounded
flag. The allowlist is not decorative: ``_sanitize`` drops any key not named in
``FIELD_ALLOWLIST`` and any string value that is not token-shaped, because the
natural way to extend this event is to lift a field off ``TurnProgress`` — which
carries ``user_message_summary`` / ``agent_response_summary``, i.e. raw
transcript prose. The guard is what stops the first such refactor from shipping
transcript text to an aggregator.

**Not carried in the turn response.** The engine hands its arm counts across the
call boundary under ``TELEMETRY_HANDOFF_KEY``; the service POPS that key before
the returned metadata is persisted onto the assistant ``case_messages`` row.
That row is readable through the transcript API, and this is monitoring data —
collected like logging data, not part of the product surface.

Deliberately NOT emitted here:

* ``org_id`` — the stream is keyed by ``case_id``, which joins to an
  organisation server-side whenever a consumer is entitled to make that join.
  Carrying it inline would raise the sensitivity of the stream for no analytic
  reach the join does not already provide.
* ``engine_dry_streak`` / ``user_dry_streak`` — pure consumer-side derivations
  over a per-turn stream. Computing them in-engine would mean a new persisted
  counter (a migration) whose only reader is offline, and a second copy of a
  number the stream already determines.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:  # pragma: no cover - typing only
    from faultmaven.modules.case.domain.models import Case

#: Bumped whenever a field changes meaning or is removed. The consumer is
#: explicitly NOT app-specific, so the payload is a contract; adding a field is
#: backwards-compatible and does not bump it.
CASE_TELEMETRY_SCHEMA_VERSION = 1

#: Own logger, pinned to INFO below. Deployments run the root logger at INFO and
#: this stream must survive a deployment that raises the root to WARNING — that
#: is precisely how the pre-existing ``grounding_assessment`` trace ended up
#: with 0 hits in 5,576 lines of a real run.
TELEMETRY_LOGGER_NAME = "faultmaven.telemetry.case"

#: Key under which the engine hands its in-flight progress-arm reading to the
#: service. Popped before persistence — see the module docstring.
TELEMETRY_HANDOFF_KEY = "_case_telemetry"

_EVENT_NAME = "case_turn"

logger = logging.getLogger(TELEMETRY_LOGGER_NAME)
# Pinned, and propagation left ON. Pinning the level makes the stream immune to
# the root level; leaving propagation on means it renders through the one root
# handler (the structlog ``ProcessorFormatter``, which merges ``extra`` via
# ``ExtraAdder``) rather than needing a second handler that could double-emit.
logger.setLevel(logging.INFO)

_diag = logging.getLogger(__name__)

#: Edge trigger for the emission-failure WARNING (see ``emit_case_turn``).
_emit_failure_reported = False


class TurnPath(str, Enum):
    """Which route consumed this turn.

    ``llm`` and ``deterministic`` are the two engine routes; the rest never
    reach the engine's turn bookkeeping and would be stream gaps without an
    explicit label. ``error`` marks a turn whose number was consumed before the
    request failed — worth a row so that a provider outage is not read as an
    idle engine, which is exactly the misattribution the ``llm_health``
    requirement exists to prevent.
    """

    LLM = "llm"
    DETERMINISTIC = "deterministic"
    TERMINAL = "terminal"
    GREETING = "greeting"
    RECLASSIFICATION = "reclassification"
    ERROR = "error"


#: Every arm ``MilestoneEngine._check_if_progress_made`` actually scores, as a
#: count. An arm missing from here is not a cosmetic gap: the turn it fires on
#: emits ``progress_made=True`` with every recorded arm 0, which is exactly the
#: shape the counter-integrity rule reads as a LYING COUNTER, and — if the arm is
#: engine-side — ``engine_advanced=False`` on a turn the engine worked. Both are
#: false accusations aimed at the engine, so the set is pinned by a test that
#: parses the predicate's own source (``test_case_telemetry``).
#:
#: ``outcome_progress`` is the one arm with no metadata key of its own: the
#: predicate reads ``outcome in (DATA_REQUESTED, HYPOTHESIS_TESTED)``.
#: ``collect_progress_arms`` derives it from the same ``outcome`` value, so it
#: is summable beside the rest.
PREDICATE_ARM_KEYS: tuple[str, ...] = (
    "milestones_completed",
    "novel_evidence_added",
    "hypotheses_generated",
    "hypotheses_validated",
    "novel_solutions_proposed",
    "novel_files_uploaded",
    "hypothesis_evidence_links_applied",
    "status_transitioned",
    "outcome_progress",
)

#: Counts carried for diagnosis that the predicate deliberately does NOT score.
#: #1136 narrowed every artifact arm to its ``novel_*`` form because the LLM
#: restates constantly; keeping the RAW count beside the novel one is what makes
#: that restatement visible — a turn with ``evidence_added: 4`` and
#: ``novel_evidence_added: 0`` is the engine re-emitting what the case already
#: holds, which no other field in the row shows.
DIAGNOSTIC_ARM_KEYS: tuple[str, ...] = (
    "evidence_added",
    "solutions_proposed",
    "files_uploaded",
)

PROGRESS_ARM_KEYS: tuple[str, ...] = PREDICATE_ARM_KEYS + DIAGNOSTIC_ARM_KEYS

#: The outcomes the predicate treats as progress in its own right.
_PROGRESS_OUTCOMES = frozenset({"data_requested", "hypothesis_tested"})

#: Arms attributable to the ENGINE. ``files_uploaded`` / ``novel_files_uploaded``
#: are the user's contribution and are excluded, which is what lets the two
#: sides be told apart. ``status_transitioned`` counts as engine work because a
#: transition is the engine acting on the case, not the user supplying data.
_ENGINE_ARM_KEYS: frozenset[str] = frozenset(PREDICATE_ARM_KEYS) - {
    "novel_files_uploaded"
}

FIELD_ALLOWLIST: frozenset[str] = frozenset(
    {
        # identity
        "event",
        "schema_version",
        "case_id",
        "turn",
        "path",
        "case_state",
        # the progress decision, and the arms it was made from
        "progress_made",
        "outcome",
        "turns_without_progress",
        "arms",
        "user_supplied_new",
        "engine_advanced",
        "gate_name",
        # input-disposition ledger
        "inputs_total",
        "inputs_disposed",
        "inputs_undisposed",
        "oldest_undisposed_input_age",
        # ask ledger
        "needs_total",
        "needs_outstanding",
        "needs_fulfilled",
        "needs_superseded",
        "needs_raised_this_turn",
        "oldest_outstanding_need_age",
        # frontier (the counterweight to a gameable engine_advanced)
        "hypothesis_count",
        "hypothesis_states",
        "causal_node_count",
        "causal_node_states",
        "evidence_count",
        "solution_proposed",
        "solution_accepted",
        "solution_verified",
        "solution_state",
        "solution_feasible",
        "mitigation_present",
        # assessment (same readings the grounding trace carries)
        "verification_status",
        "grade",
        "cause_state",
        "symptom_verified",
        "work_gate_passed",
        "is_progress_stalled",
        "mece_contested",
        "seam_divergence",
        "seam_overclaim",
        # conformance / health
        "validation_repairs",
        "repair_pattern",
        # volume proxy — separates "user went silent" from "user wrote a
        # paragraph that produced nothing", carrying no content
        "user_message_chars",
        "attachment_count",
    }
)

#: A value that is enum-, id- or flag-shaped. Prose fails it on both counts:
#: transcript summaries are long and carry spaces and punctuation.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:@/-]*$")
_MAX_TOKEN_LEN = 64


def _is_token(value: str) -> bool:
    return len(value) <= _MAX_TOKEN_LEN and bool(_TOKEN_RE.match(value))


def _scalar(value: Any) -> Any:
    """Normalise one value, or raise ``ValueError`` if it is not content-free."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Enum):
        return _scalar(value.value)
    if isinstance(value, str):
        if not _is_token(value):
            raise ValueError("non-token string")
        return value
    raise ValueError(f"unsupported value type {type(value).__name__}")


def _sanitize(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Drop anything not on the allowlist, and anything carrying content.

    Fails CLOSED — a rejected field is dropped and warned about rather than
    emitted — because the failure this guards against is leaking transcript
    prose into an aggregator, and a leak cannot be un-shipped. A field dropped
    by mistake is visible as a missing column on the very first event.
    """
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in FIELD_ALLOWLIST:
            _diag.warning("case telemetry dropped non-allowlisted field %r", key)
            continue
        if isinstance(value, Mapping):
            # Histogram keys are themselves data (an enum value, an arm name),
            # so they get the same shape check as values — a histogram keyed by
            # a free-text label would otherwise be the one place prose could
            # still ride out.
            #
            # Rejected PER ENTRY, never per mapping. Dropping the whole dict
            # over one bad member is the failure the rest of this module works
            # to avoid: a row carrying ``progress_made`` with no ``arms`` key at
            # all makes the counter-integrity rule silently UNEVALUABLE rather
            # than false, which is worse than any single wrong count.
            bucket: dict[str, Any] = {}
            for k, v in value.items():
                if not _is_token(str(k)):
                    _diag.warning(
                        "case telemetry dropped %r bucket with non-token key", key
                    )
                    continue
                try:
                    bucket[str(k)] = _scalar(v)
                except ValueError as exc:
                    _diag.warning(
                        "case telemetry dropped %r bucket %r: %s", key, str(k), exc
                    )
            clean[key] = bucket
        else:
            try:
                clean[key] = _scalar(value)
            except ValueError as exc:
                _diag.warning("case telemetry dropped field %r: %s", key, exc)
    return clean


def collect_progress_arms(metadata: Mapping[str, Any]) -> dict[str, int]:
    """Per-arm counts read off the SAME dict ``_check_if_progress_made`` scores.

    Counts, never the ids themselves: the ids are case content (filenames reach
    ``files_uploaded`` only as opaque ``file_id``s, but evidence and hypothesis
    ids are joinable back to statements) and the rules that consume this stream
    need cardinality, not identity. A bool arm (``status_transitioned``) counts
    as 0 or 1 so every arm is summable.
    """
    arms: dict[str, int] = {}
    outcome = metadata.get("outcome")
    arms["outcome_progress"] = int(
        getattr(outcome, "value", outcome) in _PROGRESS_OUTCOMES
    )
    for key in PROGRESS_ARM_KEYS:
        if key == "outcome_progress":
            continue
        value = metadata.get(key)
        if isinstance(value, bool):
            arms[key] = int(value)
        elif isinstance(value, int):
            arms[key] = value
        elif value is None:
            arms[key] = 0
        else:
            try:
                arms[key] = len(value)
            except TypeError:
                arms[key] = 0
    return arms


def _input_disposition_ledger(case: "Case") -> dict[str, int]:
    """Has every input the case received been DISPOSED of?

    Disposition is observable today in exactly one form: an ``Evidence`` row
    citing the file's ``source_file_id``. The other three dispositions the
    design calls for — ``no_signal`` (the engine looked and found nothing),
    ``duplicate``, ``classification_failed`` — have no emission surface on the
    model, so an input the engine examined and correctly found barren is
    indistinguishable here from one it never looked at.

    That is a real and quantified limit, not a rounding error. On the local
    corpus 290 of 692 uploads (41.9%) carry no citing evidence row; of the 212
    of those older than three turns, 115 (54%) arrived on cases where the engine
    demonstrably produced evidence elsewhere inside the same window — it worked,
    and simply had nowhere to record what it concluded about THAT input. So
    ``inputs_undisposed`` is a SCREEN, not a verdict, and a consumer must not
    render it as "the engine ignored N inputs". Giving the engine a way to
    declare a non-extraction disposition is an engine behaviour change and
    belongs to its own issue; this module reports what is presently knowable.
    """
    cited: set[str] = {
        e.source_file_id
        for e in getattr(case, "evidence", []) or []
        if getattr(e, "source_file_id", None)
    }
    total = 0
    disposed = 0
    oldest_age = 0
    current_turn = getattr(case, "current_turn", 0) or 0
    for f in getattr(case, "uploaded_files", []) or []:
        total += 1
        if getattr(f, "file_id", None) in cited:
            disposed += 1
            continue
        age = current_turn - (getattr(f, "uploaded_at_turn", 0) or 0)
        oldest_age = max(oldest_age, max(age, 0))
    return {
        "inputs_total": total,
        "inputs_disposed": disposed,
        "inputs_undisposed": total - disposed,
        "oldest_undisposed_input_age": oldest_age if total - disposed else 0,
    }


def _ask_ledger(case: "Case") -> dict[str, int]:
    """The ask→answer side: what the engine is still waiting on the user for.

    Read together with the input ledger this is what separates the three
    situations the single stall counter collapses into one bit — user dry with
    the engine advancing, engine dry with the user still supplying, and both
    dry. ``needs_outstanding`` counts PARTIALLY_MET alongside PENDING: a
    partially met need is still an open ask, and folding it into "fulfilled"
    would report the user as owing nothing while the engine is still blocked.
    """
    total = 0
    outstanding = 0
    fulfilled = 0
    superseded = 0
    raised_this_turn = 0
    oldest_outstanding = 0
    current_turn = getattr(case, "current_turn", 0) or 0
    for need in getattr(case, "evidence_needs", []) or []:
        total += 1
        state = getattr(getattr(need, "state", None), "value", None)
        created = getattr(need, "created_at_turn", 0) or 0
        if created == current_turn:
            raised_this_turn += 1
        if state in ("pending", "partially_met"):
            outstanding += 1
            oldest_outstanding = max(oldest_outstanding, max(current_turn - created, 0))
        elif state == "fulfilled":
            fulfilled += 1
        elif state == "superseded":
            superseded += 1
    return {
        "needs_total": total,
        "needs_outstanding": outstanding,
        "needs_fulfilled": fulfilled,
        "needs_superseded": superseded,
        "needs_raised_this_turn": raised_this_turn,
        "oldest_outstanding_need_age": oldest_outstanding,
    }


def _frontier(case: "Case") -> dict[str, Any]:
    """State-histogram snapshot — the counterweight to a gameable progress bit.

    Counts and histograms, never the per-node list the grounding trace carries:
    that list is O(nodes) per event and O(nodes x turns) per case, and no rule
    over this stream needs node identity.
    """
    hyp_states: dict[str, int] = {}
    for h in (getattr(case, "hypotheses", {}) or {}).values():
        key = getattr(getattr(h, "state", None), "value", "unknown")
        hyp_states[key] = hyp_states.get(key, 0) + 1
    node_states: dict[str, int] = {}
    for n in (getattr(case, "causal_nodes", {}) or {}).values():
        key = getattr(getattr(n, "node_state", None), "value", "unknown")
        node_states[key] = node_states.get(key, 0) + 1
    p = case.progress
    return {
        "hypothesis_count": len(getattr(case, "hypotheses", {}) or {}),
        "hypothesis_states": hyp_states,
        "causal_node_count": len(getattr(case, "causal_nodes", {}) or {}),
        "causal_node_states": node_states,
        "evidence_count": len(getattr(case, "evidence", []) or []),
        "solution_proposed": bool(p.solution_proposed),
        "solution_accepted": bool(p.solution_accepted),
        "solution_verified": bool(p.solution_verified),
        "solution_state": p.solution_state,
        "solution_feasible": p.solution_feasible,
        "mitigation_present": p.mitigation is not None,
    }


def _assessment(case: "Case") -> dict[str, Any]:
    """The grounding readings, so this event and the DEBUG grounding trace agree.

    Most values are read straight off the persisted progress blob, which the
    turn has already settled. Two are not: ``work_gate_passed`` and
    ``is_progress_stalled`` are pure predicates over ``case.hypotheses`` /
    ``case.evidence`` / the causal chain, recomputed here exactly as the
    grounding trace recomputes them — the same functions over the same
    post-turn aggregate, so the channels still cannot disagree, but the
    mechanism is recomputation rather than a shared read. Measured at
    0.067 ms/turn together on a 12-hypothesis / 40-evidence case, so the
    recompute is not worth a cache that could go stale.
    """
    from faultmaven.core.investigation.cause_assurance import CauseAssuranceGrade
    from faultmaven.core.investigation.verification_status import (
        is_progress_stalled,
        work_gate_passed,
    )
    from faultmaven.modules.case.domain.models import CauseState

    p = case.progress
    seam_divergence = p.cause_assurance == CauseAssuranceGrade.CONFIRMED and (
        p.cause_state != CauseState.IDENTIFIED or not p.symptom_verified
    )
    return {
        "verification_status": p.verification_status,
        "grade": p.cause_assurance,
        "cause_state": p.cause_state,
        "symptom_verified": bool(p.symptom_verified),
        "work_gate_passed": work_gate_passed(case),
        "is_progress_stalled": is_progress_stalled(case),
        "mece_contested": bool(p.cause_identification_contested),
        "seam_divergence": bool(seam_divergence),
        # Persisted by ``_recompute_assessment_state``; read rather than
        # recomputed so the edge-triggered WARNING and this per-turn row cannot
        # disagree about whether the standing conclusion over-claims.
        "seam_overclaim": bool(p.cause_overclaim),
    }


def build_case_turn_event(
    case: "Case",
    *,
    path: TurnPath,
    arms: Mapping[str, int] | None = None,
    gate_name: str | None = None,
    progress_made: bool = False,
    outcome: Any = None,
    validation_repairs: int = 0,
    repair_pattern: str | None = None,
    user_message_chars: int = 0,
    attachment_count: int = 0,
) -> dict[str, Any]:
    """Assemble one per-turn row. Pure; the caller emits it."""
    # Normalised to the full arm set whatever the caller passed. A row is a
    # contract for a consumer that is explicitly not app-specific, so an arm
    # must never be ABSENT — absent and zero read differently to a rule keyed on
    # "progress was claimed and every arm was 0", and a partially-populated row
    # would make that rule silently unevaluable rather than false.
    arm_counts = {k: 0 for k in PROGRESS_ARM_KEYS}
    arm_counts.update({k: int(v) for k, v in (arms or {}).items()})
    ask = _ask_ledger(case)
    engine_advanced = any(arm_counts.get(k, 0) for k in _ENGINE_ARM_KEYS) or bool(
        ask["needs_raised_this_turn"]
    )
    payload: dict[str, Any] = {
        "event": _EVENT_NAME,
        "schema_version": CASE_TELEMETRY_SCHEMA_VERSION,
        "case_id": getattr(case, "case_id", None),
        "turn": getattr(case, "current_turn", 0),
        "path": path,
        "case_state": getattr(case, "state", None),
        "progress_made": bool(progress_made),
        "outcome": outcome,
        # Read AFTER the turn's update, which is the whole point of emitting
        # from the chokepoint: the pre-existing grounding trace reports this
        # field from inside response application and therefore reports the
        # PREVIOUS turn's value.
        "turns_without_progress": getattr(case, "turns_without_progress", 0),
        "arms": arm_counts,
        # The design's ``user_supplied_new`` is defined as novel uploads and
        # nothing else. Typed prose has no upstream measurement point: deciding
        # whether a message carries new content IS the semantic judgment this
        # stream is forbidden to make, and no evidence rows are minted from it.
        "user_supplied_new": bool(arm_counts.get("novel_files_uploaded", 0)),
        "engine_advanced": bool(engine_advanced),
        "gate_name": gate_name,
        "validation_repairs": int(validation_repairs),
        "repair_pattern": repair_pattern,
        "user_message_chars": int(user_message_chars),
        "attachment_count": int(attachment_count),
    }
    payload.update(_input_disposition_ledger(case))
    payload.update(ask)
    payload.update(_frontier(case))
    payload.update(_assessment(case))
    return _sanitize(payload)


def emit_case_turn(case: "Case", **kwargs: Any) -> None:
    """Emit one row for a consumed turn. Never raises.

    A diagnostic must not be able to break the turn it observes, so every
    failure — a half-built case from a fixture, a model field renamed under it —
    degrades to a debug line.
    """
    try:
        payload = build_case_turn_event(case, **kwargs)
        logger.info(
            "case-telemetry case=%s turn=%s path=%s progress=%s twp=%s",
            payload.get("case_id"),
            payload.get("turn"),
            payload.get("path"),
            payload.get("progress_made"),
            payload.get("turns_without_progress"),
            extra=payload,
        )
    except Exception:  # noqa: BLE001 - observability must never break a turn
        # Edge-triggered at WARNING. Failure isolation must not become failure
        # INVISIBILITY: a builder broken by a renamed model field silences the
        # whole stream, and total absence of rows is indistinguishable from
        # "no turns happened" — which is precisely the level-gate failure that
        # made the DEBUG grounding trace useless. One WARNING with the traceback
        # per process says it out loud; the rest stay at DEBUG so a systematic
        # break does not also flood the log it is trying to appear in.
        global _emit_failure_reported
        if not _emit_failure_reported:
            _emit_failure_reported = True
            _diag.warning(
                "case telemetry emission failed; the stream is now silent for "
                "this process. Further failures log at DEBUG.",
                exc_info=True,
            )
        else:
            _diag.debug("case telemetry emission failed", exc_info=True)
