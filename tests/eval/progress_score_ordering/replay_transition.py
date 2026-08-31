"""Replay every stored INQUIRY->INVESTIGATING transition through the real engine.

#1270: the generation path scored ``progress_made`` before
``_check_automatic_transitions`` wrote the ``status_transitioned`` arm, so the
turn the engine moved the case on never counted as progress.

Real data, not a fixture: each replay is seeded from ONE stored case's own
title, description, ``problem_confirmation``, ``preliminary_urgency``, proposed
problem statement and its two real user messages. Only the LLM is stubbed --
with the fields the case actually recorded -- so the engine runs the same
ordering it runs in production.

Not collected by pytest (no ``test_`` filename). Reads the corpus; writes nothing.
Run from the repo root::

    python tests/eval/progress_score_ordering/replay_transition.py

Corpus override: ``FM_CASES_DB``. Sample cap for a quick pass: ``FM_LIMIT``.
"""

import asyncio
import contextlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
# Ahead of site-packages: an editable install would otherwise resolve
# ``faultmaven`` to whichever checkout it was installed from, and a replay of
# the wrong tree proves nothing. Asserted below rather than assumed.
sys.path.insert(0, str(REPO_ROOT))

import faultmaven  # noqa: E402

assert Path(faultmaven.__file__).is_relative_to(
    REPO_ROOT
), f"imported faultmaven from {faultmaven.__file__}, not from {REPO_ROOT}"

from faultmaven.core.investigation.milestone_engine import MilestoneEngine  # noqa: E402
from faultmaven.infrastructure.llm.structured_output_capability import (  # noqa: E402
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)
from faultmaven.models.interfaces import ILLMProvider  # noqa: E402
from faultmaven.modules.case.contracts import Case, CaseState, InquiryData  # noqa: E402

DB = os.environ.get("FM_CASES_DB", str(REPO_ROOT / "data" / "faultmaven.db"))

#: ``working_conclusion_generator._generate_next_steps`` emits exactly this, and
#: returns early, iff ``case.state == INQUIRY``. Progress metrics are computed
#: AFTER the transition check, so the stored ``next_steps`` witnesses the
#: POST-transition state while the stored ``progress_made`` in the same record
#: carries the PRE-transition score. That is what makes the transition turn
#: identifiable from stored history alone.
SENTINEL = "Confirm problem statement and decide to investigate"


class StubLLM(ILLMProvider):
    """Returns whatever ``payload`` currently holds, for every call shape."""

    def __init__(self) -> None:
        self.payload = "{}"

    async def generate(self, prompt, **kwargs):
        return self.payload

    async def generate_stream(self, prompt, **kwargs):
        yield self.payload

    async def generate_with_history(self, messages, **kwargs):
        return self.payload

    def get_structured_output_strategy(self, schema):
        return StructuredOutputStrategy(
            capability=StructuredOutputCapability.STRICT,
            mode=StructuredOutputMode.JSON_SCHEMA_STRICT,
            include_schema_in_prompt=False,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "S", "strict": True, "schema": schema},
            },
        )


def is_inquiry(turn: dict) -> bool:
    return (turn.get("next_steps") or []) == [SENTINEL]


def transition_index(turns: list[dict]) -> int | None:
    """Index of the INQUIRY->INVESTIGATING turn, or ``None`` if not observable."""
    for j in range(1, len(turns)):
        if is_inquiry(turns[j - 1]) and not is_inquiry(turns[j]):
            return j if all(is_inquiry(t) for t in turns[:j]) else None
    return None


def load_corpus() -> list[dict]:
    with contextlib.closing(sqlite3.connect(DB)) as conn:
        return _load_corpus(conn)


def _load_corpus(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "select case_id, user_id, organization_id, title, description, "
        "inquiry, metadata from cases where metadata is not null"
    ).fetchall()
    out = []
    for r in rows:
        meta = json.loads(r["metadata"])
        turns = sorted(
            meta.get("turn_history") or [], key=lambda t: t.get("turn_number") or 0
        )
        j = transition_index(turns)
        if j is None:
            continue
        inq = json.loads(r["inquiry"]) if r["inquiry"] else {}
        by_turn: dict[int, str] = {}
        for m in conn.execute(
            "select turn_number, content from case_messages "
            "where case_id=? and role='user' order by turn_number, created_at",
            (r["case_id"],),
        ):
            by_turn.setdefault(m["turn_number"], m["content"])
        first = turns[j - 1].get("turn_number")
        trans = turns[j].get("turn_number")
        out.append(
            dict(
                case_id=r["case_id"],
                user_id=r["user_id"] or "user_123",
                organization_id=r["organization_id"] or "org_123",
                title=r["title"] or "Case",
                description=r["description"] or "",
                inquiry=inq,
                msg_first=by_turn.get(first)
                or turns[j - 1].get("user_message_summary")
                or "Something is broken",
                msg_confirm=by_turn.get(trans)
                or turns[j].get("user_message_summary")
                or "yes",
                stored_progress_made=bool(turns[j].get("progress_made")),
            )
        )
    return out


def turn1_payload(rec: dict) -> str:
    """The case's own turn-1 proposal, replayed back as the LLM's response."""
    inq = rec["inquiry"]
    pc = inq.get("problem_confirmation") or {}
    pu = inq.get("preliminary_urgency") or {}
    stmt = (
        inq.get("proposed_problem_statement")
        or pc.get("preliminary_guidance")
        or rec["title"]
    )
    return json.dumps(
        {
            "agent_response": f"Let me confirm: {stmt}. Is that right?",
            "state_updates": {
                "problem_confirmation": {
                    "problem_type": pc.get("problem_type") or "error",
                    "severity_guess": pc.get("severity_guess") or "medium",
                    "preliminary_guidance": stmt,
                },
                "preliminary_urgency": {
                    "level": (pu.get("level") or "high").upper(),
                    "is_ongoing": bool(pu.get("is_ongoing", True)),
                    "is_incident_report": bool(pu.get("is_incident_report", True)),
                    "impact_assessment": pu.get("impact_assessment") or "impact",
                },
                "proposed_problem_statement": stmt,
                "user_confirmed_investigation": False,
            },
        }
    )


TURN2 = json.dumps(
    {
        "agent_response": "Confirmed. Starting the investigation.",
        "state_updates": {"user_confirmed_investigation": True},
    }
)


async def replay(rec: dict) -> tuple[str, bool | None, int | None]:
    llm = StubLLM()
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock()
    engine = MilestoneEngine(llm, repo, investigation_tools=MagicMock())
    case = Case(
        case_id=rec["case_id"],
        title=rec["title"][:200],
        state=CaseState.INQUIRY,
        user_id=rec["user_id"],
        organization_id=rec["organization_id"],
        description=rec["description"][:2000] if rec["description"] else "",
        inquiry=InquiryData(thread_id="thread_replay"),
    )
    llm.payload = turn1_payload(rec)
    first = await engine.process_turn(case, rec["msg_first"][:4000])
    if first["case_updated"].state != CaseState.INQUIRY:
        return ("skip_turn1_state", None, None)
    llm.payload = TURN2
    second = await engine.process_turn(first["case_updated"], rec["msg_confirm"][:4000])
    updated = second["case_updated"]
    # Positive control: a replay that did not transition proves nothing about
    # how a transition is scored, so it is reported as skipped, not as a pass.
    if updated.state != CaseState.INVESTIGATING:
        return ("skip_no_transition", None, None)
    return (
        "ok",
        bool(second["metadata"].get("progress_made")),
        updated.turns_without_progress,
    )


async def main() -> None:
    corpus = load_corpus()
    limit = int(os.environ.get("FM_LIMIT", "0"))
    if limit:
        corpus = corpus[:limit]
    scored_true = scored_false = 0
    twp: dict[int, int] = {}
    skipped: dict[str, int] = {}
    errors: list[tuple[str, str]] = []
    for rec in corpus:
        try:
            status, made, counter = await replay(rec)
        except Exception as exc:  # noqa: BLE001 - a broken case is data, not a crash
            errors.append((rec["case_id"], f"{type(exc).__name__}: {exc}"))
            continue
        if status != "ok":
            skipped[status] = skipped.get(status, 0) + 1
            continue
        if made:
            scored_true += 1
        else:
            scored_false += 1
        twp[counter] = twp.get(counter, 0) + 1

    total = scored_true + scored_false
    # DENOMINATOR FIRST. "0 mis-scored" means nothing without it: a detector
    # that silently matched no turns prints a perfect zero that actually says
    # "I could not ask". Three outcomes, never two -- fixed / not fixed /
    # could not ask -- so a run that found nothing to replay says so loudly
    # and exits non-zero rather than reading as a pass.
    print(f"transition cases FOUND in the corpus  : {len(corpus)}")
    print(f"transition cases REPLAYED (denominator): {total}")
    if not corpus or not total:
        print(
            "COULD NOT ASK: the detector matched no replayable transition turn, "
            "so the numerator below is meaningless."
        )
        if skipped:
            print(f"  skipped (replay did not transition) : {skipped}")
        if errors:
            print(f"  errors: {len(errors)}")
            for case_id, message in errors[:5]:
                print(f"    {case_id}: {message[:200]}")
        raise SystemExit(2)
    print(f"  transition turn progress_made=True  : {scored_true}")
    print(f"  transition turn progress_made=False : {scored_false}")
    print(f"  turns_without_progress after the turn: {sorted(twp.items())}")
    if skipped:
        print(f"  skipped (replay did not transition) : {skipped}")
    if errors:
        print(f"  errors: {len(errors)}")
        for case_id, message in errors[:5]:
            print(f"    {case_id}: {message[:200]}")


if __name__ == "__main__":
    asyncio.run(main())
