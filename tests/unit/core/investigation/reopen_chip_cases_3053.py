"""The reopen chip's replies: their fixture corpus (#3053 S3d-2).

One case per reply the chip can give: the confirmation on a READY case (each
cause-identification leg, with solutions, under a standing decline), each
refusal (a pending check, held or FULFILLED from before the ask; a failed fix;
an off-cause fix; nothing on record; each documentation-gaps ask, the cause
re-check's names included, and the "Root cause" ask whose record gives a
pool-empty line) and the hold's (a false alarm, a revision pending). Every
fixture is built from the suite's builders.

``render`` runs the chip (``_resolve_on_reopen_chip``) and returns what the
user is shown, the reply and its chips, and whether an offer opened. Its
output on ``main`` is ``reopen_chip_oracle_3053.json``, captured by running
``render_all`` with this file copied into a ``git archive`` extract of
``main`` at babd5b944 (as ``reply_cases_2811`` was), never typed by hand.
Ids (offer ids included) are deterministic and the clock is frozen while a
fixture is built and rendered.
"""

from __future__ import annotations

import json
import random
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from tests.unit.core.investigation import golden_cases_2636 as golden
from tests.unit.core.investigation import offer_cases_2811 as oc
from tests.unit.core.investigation.prompts import prompt_state_cases_2811 as ps
from tests.unit.core.investigation.prompts.prompt_state_cases_2811 import frozen


def _no_cause():
    return golden._with_evidence(golden._bare_case())


#: Every fixture, by name.
CASES = {
    # READY: the confirmation, and the offer opened
    "ready:confirmed": golden._confirmed_case,
    "ready:no_check_asked": lambda: golden._held(asked=False),
    "ready:declined": lambda: golden._declined(golden._held(asked=False)),
    "ready:no_cause_leg_no_solutions": golden._no_cause_leg_no_solutions,
    "ready:named_with_solutions": lambda: golden._with_solutions(
        golden._concluded(golden._confirmed_case()), "raise the limit", ""
    ),
    # PENDING_CHECK: the held check's ask, the chips kept
    "refused:pending_check": golden._held,
    "refused:pending_check_declined": lambda: golden._declined(golden._held()),
    "refused:pending_check_fulfilled": lambda: golden._held(fulfilled=True),
    # The other refusals: "no longer stands" and the readiness ask
    "refused:fix_failed": golden._failed_fix,
    "refused:fix_off_cause": golden._interception_only,
    "refused:nothing_on_record": golden._bare_case,
    "refused:gaps_no_cause_pool_empty": _no_cause,
    "refused:gaps_held_open_pool_empty": oc._held_open,
    "refused:gaps_identified": lambda: golden._identified_case()[0],
    "refused:gaps_relief_named": golden._relief_only,
    "refused:gaps_relief_unnamed": golden._relief_unnamed,
    "refused:gaps_relief_names_clip_empty": lambda: golden._relief_clipped_empty(
        ".", "..."
    ),
    "refused:gaps_cause_leg": golden._cause_leg_only,
    "refused:gaps_no_symptom_row": lambda: golden._no_symptom_row(
        golden._identified_case()[0]
    ),
    "refused:gaps_pair": lambda: golden._no_symptom_row(golden._cause_leg_only()),
    # On hold: the hold's reply, before any read of the record
    "hold:false_alarm": golden._invalidated,
    "hold:revision_pending": lambda: golden._declined(
        golden._confirmed_case(), on_hold=True
    ),
}


@contextmanager
def _deterministic(seed: str):
    """Deterministic ids (an offer's ``proposal_id`` too), the clock frozen."""
    from faultmaven.core.investigation import offers

    draw = random.Random(seed)
    token_hex = lambda n: draw.randbytes(n).hex()  # noqa: E731
    with (
        ps.fv._deterministic_ids(seed),
        patch.object(offers, "secrets", SimpleNamespace(token_hex=token_hex)),
        frozen(),
    ):
        yield


def build(name: str):
    """The fixture ``name``, built with deterministic ids, the clock frozen."""
    with _deterministic(name):
        return CASES[name]()


def chip(case) -> dict:
    """The chip's reply on ``case``, as returned."""
    from faultmaven.core.investigation.milestone_engine.transition_turns import (
        _resolve_on_reopen_chip,
    )

    return _resolve_on_reopen_chip(case=case, upload_report={}, user_message="")


def render(name: str) -> dict:
    """What the user is shown for the fixture ``name``, and whether an offer
    opened, as JSON reads it back."""
    case = build(name)
    with _deterministic(f"{name}:chip"):
        reply = chip(case)
    out = {
        "agent_response": reply["agent_response"],
        "suggested_follow_ups": reply["suggested_follow_ups"],
        "opened": case.offer_record.open is not None,
    }
    return json.loads(json.dumps(out, default=str))


def render_all() -> dict[str, dict]:
    """Each fixture's render, by name."""
    return {name: render(name) for name in CASES}
