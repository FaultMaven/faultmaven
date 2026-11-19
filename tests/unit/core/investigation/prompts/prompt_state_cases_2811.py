"""The prompt's milestone status, exclusion rule and focus emphasis: their
fixture corpus (#2811 S2b-1c-i).

The ``<evidence_needs>`` corpus (``evidence_needs_cases_2811.CASES``, 67
fixtures), plus one per line none of them renders: an INQUIRY case (none of
the three is shown); each stage gate (``solution_verified``, a mitigation's
disposition); the symptom's observation dated stale and dated current (the
currency note and Zone 2's two variants); a false alarm whose close was
declined; a standing resolve decline given on an earlier turn, with the
reopen chip and with an offer open (no chip); and the exclusion rule on an
unverified problem (no action half) and on a revision pending (cause work
staged). Every added fixture is hand-built: none makes a claim that a turn
reaches it.

``render_all`` renders each through ``main``'s entry point,
``get_prompt_for_case``: the context's ``milestones`` and ``exclusions``
sections, and the ``focus_emphasis`` the template is given, with no
processing mode and as a ``knowledge_query`` turn (which carries only the
resolve-decline lines). Its output on ``main`` is
``prompt_state_oracle_2811.json``, captured at 19ab0e8b8 by running
``render_all`` with this file copied into a ``git archive`` extract of that
commit, never typed by hand. The clock is frozen at ``NOW`` while a fixture
is built and rendered (``frozen``).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from freezegun import freeze_time

from faultmaven.modules.case.contracts import (
    CaseState,
    InvestigationStage,
    MitigationRecord,
    ProblemStatus,
)
from tests.unit.core.investigation import test_narrator_record_parity_2811 as parity
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN
from tests.unit.core.investigation.prompts import evidence_needs_cases_2811 as en
from tests.unit.core.investigation.prompts import fix_verification_cases_2811 as fv
from tests.unit.core.investigation.test_evidence_needs_context_block import (
    _make_case,
)

#: The instant every fixture is built and rendered at.
NOW = "2026-11-19T00:00:00+00:00"

#: The processing modes rendered: none, and one that carries only the
#: resolve-decline lines.
MODES = {"focus": None, "focus_knowledge_query": "knowledge_query"}


async def _inquiry():
    return _make_case(state=CaseState.INQUIRY)


async def _solution_verified():
    from tests.unit.core.investigation.test_exclusion_narration_1911 import _case

    case = _case(InvestigationStage.TREATMENT, key_refuted=True)
    case.progress.solution_verified = True
    return case


async def _disposition():
    """A declared-stable mitigation, on a problem verified with its
    verification record (so no other declared difference shows)."""
    from tests.unit.core.investigation.test_symptom_verification_currency import (
        _case,
    )

    case = _case()
    case.progress.mitigation = MitigationRecord(
        proposed_at_turn=4,
        accepted=True,
        disposition="declared_stable",
        disposition_at_turn=5,
    )
    return case


async def _observed(age: timedelta):
    """A verified problem whose one symptom row was observed ``age`` before
    ``NOW``."""
    from tests.unit.core.investigation.test_symptom_verification_currency import (
        _case,
        _symptom,
    )

    return _case(evidence=[_symptom(datetime.fromisoformat(NOW) - age)])


async def _false_alarm_declined():
    from tests.case_builders import with_declined_offer
    from tests.unit.core.investigation.test_declined_close_binds_the_model_1889 import (
        _invalidated_case,
    )

    case = _invalidated_case()
    case.current_turn = 6
    with_declined_offer(case, kind="close", basis="false_alarm")
    return case


async def _declined_earlier(*, offer_open: bool = False):
    """The resolve offer declined at the case's turn, read a turn later; with
    ``offer_open``, a close offer opened since, which takes the chip away."""
    from tests.case_builders import with_open_offer

    case = await parity._offer_answered_at_0b()
    case.current_turn += 1
    if offer_open:
        with_open_offer(case, kind="close", basis="model")
    return case


async def _hypotheses(status: ProblemStatus):
    """The exclusion record's case (a validated cause, a refuted alternative)
    on a problem at ``status``."""
    from tests.unit.core.investigation.test_exclusion_narration_1911 import _case

    case = _case(key_refuted=True)
    case.progress.problem_status = status
    return case


async def _revision_with_hypotheses():
    from tests.unit.core.investigation.test_current_record_2811 import (
        _revision_pending,
    )
    from tests.unit.core.investigation.test_exclusion_narration_1911 import _case

    case = _revision_pending()
    case.hypotheses.update(_case(key_refuted=True).hypotheses)
    return case


#: The fixtures this corpus adds, one per line no fixture above shows.
ADDED = {
    "P:inquiry": _inquiry,
    "P:solution_verified": _solution_verified,
    "P:disposition": _disposition,
    "P:stale": lambda: _observed(timedelta(hours=2)),
    "P:current": lambda: _observed(timedelta(minutes=5)),
    "P:false_alarm_declined": _false_alarm_declined,
    "P:declined_earlier": _declined_earlier,
    "P:declined_offer_open": lambda: _declined_earlier(offer_open=True),
    "P:unverified_hypotheses": lambda: _hypotheses(ProblemStatus.UNVERIFIED),
    "P:revision_hypotheses": _revision_with_hypotheses,
}

#: Every fixture, by name: the ``<evidence_needs>`` corpus, then the added ones.
CASES = {**en.CASES, **ADDED}


@contextmanager
def frozen():
    """The clock at ``NOW``."""
    with freeze_time(NOW):
        yield


async def build(name: str):
    """The fixture ``name``, built with deterministic ids at ``NOW``."""
    with fv._deterministic_ids(name), frozen():
        return await CASES[name]()


def render(case) -> dict:
    """What ``get_prompt_for_case`` puts in the three places, by mode: the
    ``milestones`` and ``exclusions`` sections of the context it builds (the
    last build, with no processing mode), and the ``focus_emphasis`` the
    INVESTIGATING template is given (None where it renders another
    template)."""
    from faultmaven.core.investigation.prompts.templates import (
        assembly as templates,
    )

    seen: dict = {}
    real_ctx, real_base = templates.build_investigation_context, (
        templates.INVESTIGATION_BASE
    )

    def ctx(*args, **kwargs):
        built = real_ctx(*args, **kwargs)
        seen["milestones"], seen["exclusions"] = (
            built["milestones"],
            built["exclusions"],
        )
        return built

    class _Base:
        def format(self, **fields):
            seen["focus"] = fields["focus_emphasis"]
            return real_base.format(**fields)

    out: dict = {}
    with (
        patch.object(templates, "build_investigation_context", ctx),
        patch.object(templates, "INVESTIGATION_BASE", _Base()),
        frozen(),
    ):
        for key, mode in MODES.items():
            seen.clear()
            templates.get_prompt_for_case(
                case, "status", given_tools=DEFAULT_GIVEN, processing_mode=mode
            )
            if mode is None:
                out["milestones"] = seen["milestones"]
                out["exclusions"] = seen["exclusions"]
            out[key] = seen.get("focus")
    return out


async def render_all() -> dict[str, dict]:
    """Each fixture's three places, by name."""
    return {name: render(await build(name)) for name in CASES}
