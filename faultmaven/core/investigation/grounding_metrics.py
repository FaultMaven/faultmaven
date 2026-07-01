"""Phase 0b — the both-arms grounding baseline metric.

Rolls the drift-locked per-case counter (``cause_assurance.count_grounded_roots``) up
over a case population, applying the non-circular R3 denominator (cases where a
v4-matchable runbook was found, captured pre-verdict via ``Case.runbook_retrieved``) and
the two ratified projections (all-INVESTIGATING vs terminal-only). Contract:
``docs/architecture/investigation-engine/grounded-cause-counting.md``.

Why it exists: to make a dead mechanism *visible*. Today both grounding arms read ~0 over
a real, non-zero denominator — the runbook arm because the differential is seeded only on
a rare 'single' verdict (RC-1), the deductive arm because it is unwired (#593). That
near-zero numerator over a non-zero denominator is the finding; a 0/0 would only mean the
metric is mis-scoped, which is exactly the R3 failure mode this denominator avoids. When
Part A lands, ``runbook_arm_roots`` moves off 0; when #593 lands, ``deductive_arm_roots``
moves off 0 — the per-arm split is what keeps either fix from masking the other's dormancy.

Storage-agnostic by design: takes an iterable of hydrated ``Case`` objects so it is pure
and unit-testable. A driver (script / metrics hook) supplies the population from the case
store.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from faultmaven.core.investigation.cause_assurance import count_grounded_roots

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case

GroundingScope = Literal["all", "terminal"]


@dataclass(frozen=True)
class GroundingBaseline:
    """Aggregate grounding counts over a scoped, matching-runbook case population.

    Per-arm root counts are set-union-safe: ``grounded_roots`` is NOT
    ``runbook_arm_roots + deductive_arm_roots`` (the arms are non-exclusive — a root can
    be grounded by both). Report ``grounded_roots`` directly.
    """

    scope: GroundingScope
    population: int
    """R3 denominator: cases with a runbook retrieved (``runbook_retrieved``), further
    filtered to terminal dispositions when ``scope == 'terminal'``. NON-circular — never
    sourced from the verdict-gated ``differential_runbook_ids``."""

    cases_grounded: int
    """Cases with >=1 grounded root (case-level ``GROUNDED``)."""

    grounded_roots: int
    """Validated roots grounded by either arm (set-union). MUST NOT be summed from the
    two arm counts below."""

    runbook_arm_roots: int
    """Grounded roots via the runbook arm. Off 0 ⇒ Part A landed."""

    deductive_arm_roots: int
    """Grounded roots via the deductive arm. Off 0 ⇒ #593 landed. A live 0 today is the
    unwired arm, not a metric gap."""

    runbook_links_fired: int
    """Leading indicator: runbook predicates fired on ANY node (root or intermediate).
    Diagnostic only — never a grounding count. Moves off 0 before ``runbook_arm_roots``
    when Part A goes live."""

    @property
    def grounded_rate(self) -> float:
        """Cases grounded / population. 0.0 on an empty population (no matching-runbook
        cases), which is a mis-scope signal, NOT a grounding reading."""
        return self.cases_grounded / self.population if self.population else 0.0


def _in_population(case: "Case", scope: GroundingScope) -> bool:
    """R3 denominator membership. Gate-independent: keyed on ``runbook_retrieved`` (the
    pre-verdict retrieval marker), never on the verdict-gated ``differential_runbook_ids``.
    'reached INVESTIGATING' is implied by ``runbook_retrieved`` — the matcher only runs in
    INVESTIGATING — so the 'all' scope needs no extra state test."""
    if not case.runbook_retrieved:
        return False
    if scope == "terminal":
        # Harvest-readiness view; reuse the single terminal-set owner (CaseState).
        return case.state.is_terminal
    return True


def compute_grounding_baseline(
    cases: Iterable["Case"], *, scope: GroundingScope = "all"
) -> GroundingBaseline:
    """Roll ``count_grounded_roots`` up over ``cases`` within the scoped, matching-runbook
    population. See module docstring for the projections and their meaning.

    ``cases`` is consumed exactly once. To report both scopes ('all' and 'terminal') of
    the same population, pass a re-iterable collection (list/tuple), NOT a one-shot
    generator — a generator would be exhausted by the first call and the second scope
    would read an empty population."""
    population = 0
    cases_grounded = 0
    grounded_roots = 0
    runbook_arm_roots = 0
    deductive_arm_roots = 0
    runbook_links_fired = 0

    for case in cases:
        if not _in_population(case, scope):
            continue
        population += 1
        tally = count_grounded_roots(case)
        grounded_roots += tally.grounded_roots
        runbook_arm_roots += tally.runbook_arm
        deductive_arm_roots += tally.deductive_arm
        runbook_links_fired += tally.runbook_links_fired
        if tally.grounded_roots >= 1:
            cases_grounded += 1

    return GroundingBaseline(
        scope=scope,
        population=population,
        cases_grounded=cases_grounded,
        grounded_roots=grounded_roots,
        runbook_arm_roots=runbook_arm_roots,
        deductive_arm_roots=deductive_arm_roots,
        runbook_links_fired=runbook_links_fired,
    )
