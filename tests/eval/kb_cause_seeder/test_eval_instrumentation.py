"""CI unit tests for the KB cause-seeder eval *instrumentation* logic.

The eval driver (`run_seed_eval.py`) needs a live server + provider key + flag
and is NOT collected by CI. But its instrumentation math — the three-state
assertion recorder and the held-over-*exercised* aggregation — is pure logic that
decides whether a batch reads as safe, so it is pinned here. This file matches
`test_*.py` and runs in CI with no live server.

Run: pytest tests/eval/kb_cause_seeder/test_eval_instrumentation.py
"""

import importlib.util
from pathlib import Path

_DIR = Path(__file__).parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


run_seed_eval = _load("run_seed_eval")
aggregate_runs = _load("aggregate_runs")


# ---------------------------------------------------------------------------
# Three-state Checks recorder
# ---------------------------------------------------------------------------
def test_gate_states_map_from_exercised_and_held():
    chk = run_seed_eval.Checks()
    chk.gate("held gate", True, exercised=True)
    chk.gate("breached gate", False, exercised=True)
    chk.gate(
        "unexercised gate", False, exercised=False
    )  # held ignored when not exercised
    chk.gate("unexercised held gate", True, exercised=False)
    states = {i["name"]: i["state"] for i in chk.to_list()}
    assert states["held gate"] == run_seed_eval.HELD
    assert states["breached gate"] == run_seed_eval.BREACHED
    # not-exercised wins regardless of the held value
    assert states["unexercised gate"] == run_seed_eval.NOT_EXERCISED
    assert states["unexercised held gate"] == run_seed_eval.NOT_EXERCISED


def test_ok_fails_only_on_breached_gate():
    # A breached gate fails the run.
    breached = run_seed_eval.Checks()
    breached.gate("g", False, exercised=True)
    assert breached.ok() is False

    # A not-exercised gate does NOT fail the run (precondition never arose).
    notex = run_seed_eval.Checks()
    notex.gate("g", False, exercised=False)
    assert notex.ok() is True

    # A breached *measurement* does NOT fail the run (measurements never gate).
    meas = run_seed_eval.Checks()
    meas.measure("m", False, exercised=True)
    assert meas.ok() is True

    # All held → pass.
    allheld = run_seed_eval.Checks()
    allheld.gate("g", True, exercised=True)
    allheld.measure("m", True, exercised=True)
    assert allheld.ok() is True


def test_to_list_preserves_kind_and_order():
    chk = run_seed_eval.Checks()
    chk.gate("first", True)
    chk.measure("second", True)
    items = chk.to_list()
    assert [i["name"] for i in items] == ["first", "second"]
    assert items[0]["kind"] == "gate"
    assert items[1]["kind"] == "measurement"


# ---------------------------------------------------------------------------
# Aggregation: held-rate over EXERCISED runs (NOT-EXERCISED excluded)
# ---------------------------------------------------------------------------
def _run(mode, states, result, **meta):
    """Build a minimal --json run payload. `states` maps assertion name ->
    (kind, state)."""
    md = {
        "mode": mode,
        "commit": "abc",
        "provider": "fireworks",
        "flag_env": "true",
        "seeding_observed": True,
        "crashed_at_turn": None,
    }
    md.update(meta)
    return {
        "metadata": md,
        "assertions": [
            {"name": n, "kind": k, "state": s} for n, (k, s) in states.items()
        ],
        "result": result,
    }


def test_summarize_excludes_not_exercised_from_denominator():
    H, B, N = run_seed_eval.HELD, run_seed_eval.BREACHED, run_seed_eval.NOT_EXERCISED
    # Reproduces the historical mislead batch: 3b-pos exercised on 6 of 8 runs
    # (held on all 6), and one dup breach → 7/8 pass.
    runs = []
    for _ in range(5):
        runs.append(
            _run("mislead", {"dup": ("gate", H), "3b-pos": ("measurement", H)}, "pass")
        )
    for _ in range(2):
        runs.append(
            _run("mislead", {"dup": ("gate", H), "3b-pos": ("measurement", N)}, "pass")
        )
    runs.append(
        _run("mislead", {"dup": ("gate", B), "3b-pos": ("measurement", H)}, "fail")
    )

    summary = aggregate_runs.summarize(runs)
    m = summary["mislead"]
    assert m["n_runs"] == 8
    assert m["n_pass"] == 7
    rows = {r["name"]: r for r in m["assertions"]}

    dup = rows["dup"]
    assert dup["held"] == 7 and dup["breached"] == 1 and dup["exercised"] == 8

    pos = rows["3b-pos"]
    # held on 6, NOT-EXERCISED on 2 → denominator is the 6 exercised, not 8.
    assert pos["held"] == 6 and pos["breached"] == 0
    assert pos["not_exercised"] == 2 and pos["exercised"] == 6


def test_summarize_groups_by_mode_and_counts_crashes():
    runs = [
        _run("smoke", {"seed": ("gate", run_seed_eval.HELD)}, "pass"),
        _run(
            "mislead",
            {"dup": ("gate", run_seed_eval.HELD)},
            "pass",
            crashed_at_turn=2,
            seeding_observed=False,
        ),
    ]
    summary = aggregate_runs.summarize(runs)
    assert set(summary) == {"smoke", "mislead"}
    assert summary["mislead"]["n_crash"] == 1
    assert summary["mislead"]["n_seeding"] == 0
    assert summary["smoke"]["n_seeding"] == 1


def test_tally_mode_defaults_missing_kind_to_gate():
    rows = aggregate_runs.tally_mode(
        [{"assertions": [{"name": "x", "state": run_seed_eval.HELD}]}]
    )
    assert rows[0]["kind"] == "gate"


# ---------------------------------------------------------------------------
# smoke-degenerate — in-process, deterministic (runs in CI without a server)
# ---------------------------------------------------------------------------
# Unlike the LLM-driven modes, smoke-degenerate drives the REAL seeder in-process
# on crafted malformed causes, so it is deterministic and DOES belong in CI: it
# pins that each degenerate shape is rejected with its exact SkipClass and that a
# control good cause still seeds. (The corpus doubles as the Phase-5 fixture set.)
def test_smoke_degenerate_classifies_every_bad_cause_and_seeds_control():
    meta = {}
    chk, measurements = run_seed_eval.run_smoke_degenerate(meta)

    # All gates held — deterministic, so nothing may be BREACHED or NOT-EXERCISED.
    assert chk.ok()
    states = {i["name"]: i["state"] for i in chk.to_list()}
    assert all(s == run_seed_eval.HELD for s in states.values()), states

    # Corpus covers all three rejection classes; only the control seeds.
    assert measurements["degenerate_causes"] == 10
    assert measurements["skips_by_class"] == {
        "intentional": 1,
        "quality_drop": 4,
        "unsupported_shape": 5,
    }
    assert measurements["control_seeded"] == 1
    assert meta["seeding_observed"] is True


def test_degenerate_corpus_expectations_are_well_formed():
    # Every entry maps to a real SkipClass value and uses a distinct cause_letter
    # (the seeder keys skips on letter; a collision would hide a miss).
    from faultmaven.core.investigation.kb_cause_seeder import SkipClass

    valid = {c.value for c in SkipClass}
    letters = [d["cause"]["cause_letter"] for d in run_seed_eval.DEGENERATE_CAUSES]
    assert len(letters) == len(set(letters))  # no duplicate letters
    assert all(d["expected"] in valid for d in run_seed_eval.DEGENERATE_CAUSES)
    assert run_seed_eval.CONTROL_CAUSE["cause_letter"] not in letters
