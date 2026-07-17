#!/usr/bin/env python3
"""Flag-ON KB cause-seeder enabling eval — live-server, LLM-agnostic driver.

This is the re-runnable artifact behind the seeder's *enabling gate* (see
`docs/architecture/knowledge-and-ai/kb-cause-seeder.md` → "Enabling gate"). It
drives real cases through a running FaultMaven server with
`FAULTMAVEN_KB_CAUSE_SEEDER=true`, then makes **mechanical assertions on the
causal-graph state** dumped from `GET /debug/cases/{id}/causal-graph`. Pass/fail
never depends on model wording — every check reads engine state (node/hypothesis
`state`, `validation_method`, `belief`, likelihood), so a weaker model changes
*whether a scenario's precondition is reachable*, never the rule being asserted
(cross-cutting rule: model variation never changes engine rules).

This does **not** run in CI: it needs a live server, a real provider key, and the
flag ON. It is committed so the enabling-gate claim rests on a runnable artifact,
not a doc assertion. See the sibling `README.md` for how to stand up the server
and the recorded per-scenario results.

Instrumentation (why every assertion carries three states)
----------------------------------------------------------
An LLM-driven run only *exercises* an assertion when the model reaches the state
the assertion is about. A soundness gate that never fired because the model never
engaged is **NOT-EXERCISED** — vacuously green, but no evidence of safety. A
run that reached the state and stayed sound is **HELD**; one that reached it and
broke the rule is **BREACHED**. Reporting the three apart (and, in the averager,
the exercised-rate) is what stopped a single lucky run from reading as a clean
pass. Each check therefore declares an *exercised-predicate* alongside its
held-condition, and the per-run JSON (`--json`) carries all three so
`aggregate_runs.py` can report held-rate over **exercised** runs only.

Usage:
    python run_seed_eval.py <base_url> <mode> [--dump PATH] [--json PATH]

Modes:
    smoke      seed + shape assertions (deterministic; provider-independent)
    mislead    contradict the seeded causes, point off-seed → no-collapse,
               no-crowd-out, <=1 ACTIVE/root, 3b-neg (soundness) split from
               3b-pos (engagement), plus differential-hygiene measurement
    exclusion  refute all-but-one seeded sibling and pressure toward the last
               one → the deductive-exclusion arm never fabricates a VALIDATED
               seeded cause (H1 exclusion-under-seeding probe)
    postturn1  vague-open → clarify: measures the one-shot seeding boundary

Exit code is 0 iff no *gate* assertion is BREACHED (NOT-EXERCISED gates and
measurement-only modes exit 0 — the enabling decision keys on the averaged
exercised/held rates, not any one run). The mid-scenario crash-tolerant driver
still dumps and asserts the final graph after a turn 500s, so a collapse that
also crashes the turn is caught rather than masked.
"""

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone

import httpx

# --- Engine constants mirrored here (source of truth in the engine) ----------
# Kept in sync with the engine by intent; the values are stable and documented.
#   KB_SEED_PRIOR / NEW_HYPOTHESIS_MAX_PRIOR  → core/investigation/kb_cause_seeder.py
#                                               + hypothesis_manager.py
#   MAX_SEEDED_CAUSES                          → kb_cause_seeder.py (derived, = 3)
#   DEDUCTIVE_EXCLUSION_MAX_BELIEF             → causal_graph.py (= 0.05)
KB_SEED_PRIOR = 0.3
SEED_PRIOR_CAP = 0.5
MAX_SEEDED_CAUSES = 3
DEDUCTIVE_EXCLUSION_MAX_BELIEF = 0.05

# The ArgoCD sync-failure runbook these scenarios retrieve (content-derived, so
# stable across pack rebuilds unless the runbook body changes).
ARGOCD_RUNBOOK_ID = "kb_c350de1303f6"

# Three assertion states (see the module docstring).
HELD = "HELD"
BREACHED = "BREACHED"
NOT_EXERCISED = "NOT_EXERCISED"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _client(base, token=None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=base, headers=h, timeout=300.0)


def login(base):
    with _client(base) as c:
        r = c.post("/api/v1/auth/dev-login", json={"username": "admin"})
        r.raise_for_status()
        return r.json()["access_token"]


def turn(c, case_id, query):
    r = c.post(f"/api/v1/cases/{case_id}/turns", data={"query": query})
    r.raise_for_status()
    return r.json()


def graph(c, case_id):
    r = c.get(f"/debug/cases/{case_id}/causal-graph")
    r.raise_for_status()
    return r.json()


def safe_graph(c, case_id):
    """Fetch the causal graph without raising — returns (graph|None, error|None).

    A turn can 500 mid-scenario (the very no-collapse-under-pressure case this
    eval exists to catch); we still want the final engine state. If even the
    debug read fails, the caller records that as a hard failure rather than a
    masked pass."""
    try:
        return graph(c, case_id), None
    except httpx.HTTPStatusError as e:
        return None, f"{e.response.status_code}: {e.response.text[:200]}"
    except httpx.HTTPError as e:
        return None, str(e)


def case_state(c, case_id):
    r = c.get(f"/api/v1/cases/{case_id}")
    r.raise_for_status()
    d = r.json()
    return d.get("state") or d.get("status") or d.get("case_status")


def _safe_turn(c, case_id, query, meta):
    """Send a turn, recording (not raising) a mid-scenario crash.

    Returns True on success, False if the turn failed. On failure the caller
    stops sending further turns but still asserts on the final graph — a turn
    that 500s under contradiction pressure must not leave the graph in a
    collapsed/inconsistent state, and that is exactly what the post-crash
    assertions verify."""
    meta["turns_attempted"] = meta.get("turns_attempted", 0) + 1
    idx = meta["turns_attempted"] - 1
    try:
        turn(c, case_id, query)
        return True
    except httpx.HTTPStatusError as e:
        crash = {
            "turn_index": idx,
            "query": query[:80],
            "status_code": e.response.status_code,
            "body": e.response.text[:300],
        }
        meta.setdefault("crashes", []).append(crash)
        if meta.get("crashed_at_turn") is None:
            meta["crashed_at_turn"] = idx
        print(
            f"  !! turn {idx} crashed: HTTP {crash['status_code']} — "
            f"continuing to final-graph assertions"
        )
        return False
    except httpx.HTTPError as e:
        meta.setdefault("crashes", []).append(
            {"turn_index": idx, "query": query[:80], "error": str(e)}
        )
        if meta.get("crashed_at_turn") is None:
            meta["crashed_at_turn"] = idx
        print(f"  !! turn {idx} error: {e} — continuing to final-graph assertions")
        return False


# ---------------------------------------------------------------------------
# Run metadata (stamped into every summary + JSON so recorded runs are
# self-describing: which commit, provider/model, and flag produced them)
# ---------------------------------------------------------------------------
def _git_commit():
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            timeout=5,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _server_provider(base):
    """Provider/model the *running server* resolved (more trustworthy than the
    driver's own env, which may differ from how the server was launched)."""
    try:
        with _client(base) as c:
            r = c.get("/debug/llm-providers")
            r.raise_for_status()
            d = r.json()
            pb = d.get("prompt_budget") or {}
            return d.get("primary_provider"), pb.get("model")
    except Exception:
        return None, None


def build_metadata(base, mode):
    provider, model = _server_provider(base)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "commit": _git_commit(),
        "base_url": base,
        "mode": mode,
        # The server's resolved provider/model; falls back to the driver env.
        "provider": provider or os.environ.get("CHAT_PROVIDER"),
        "model": model,
        # The flag as the driver sees it (behavioral ground-truth = seeding_observed).
        "flag_env": os.environ.get("FAULTMAVEN_KB_CAUSE_SEEDER"),
        "case_id": None,
        "turns_attempted": 0,
        "crashed_at_turn": None,
        "crashes": [],
        "seeding_observed": None,
    }


# ---------------------------------------------------------------------------
# Graph accessors (all read engine state, never LLM prose)
# ---------------------------------------------------------------------------
def seeded_hyps(g):
    return {
        hid: h
        for hid, h in g["hypotheses"].items()
        if (h.get("rationale") or "").startswith("Seeded from runbook")
    }


def seeded_runbook_ids(g):
    ids = set()
    for h in g["hypotheses"].values():
        r = h.get("rationale") or ""
        if r.startswith("Seeded from runbook "):
            ids.add(r.split("Seeded from runbook ", 1)[1].split(" ", 1)[0])
    return ids


def active_root_dups(g):
    roots = Counter(
        h["root_node_id"]
        for h in g["hypotheses"].values()
        if h["state"] == "active" and h["root_node_id"]
    )
    return {r: n for r, n in roots.items() if n > 1}


def root_nodes(g):
    return {
        nid: n
        for nid, n in g["causal_nodes"].items()
        if isinstance(n, dict) and n.get("node_type") == "root"
    }


def n_active(g):
    return sum(1 for h in g["hypotheses"].values() if h["state"] == "active")


def n_refuted(g):
    return sum(1 for h in g["hypotheses"].values() if h["state"] == "refuted")


def non_seeded_hyps(g, seeded_roots):
    return [
        h
        for h in g["hypotheses"].values()
        if h["root_node_id"] and h["root_node_id"] not in seeded_roots
    ]


# ---------------------------------------------------------------------------
# Three-state assertion recorder
# ---------------------------------------------------------------------------
class Checks:
    """Records gate + measurement assertions as one of {HELD, BREACHED,
    NOT_EXERCISED}. A gate that is BREACHED fails the run; a NOT_EXERCISED gate
    does not (the precondition was never reached — honest, not a pass). The
    exercised-rate is what the averager keys on."""

    def __init__(self):
        self._items = []  # (name, kind, state)

    def _add(self, name, kind, held, exercised):
        if not exercised:
            state = NOT_EXERCISED
        else:
            state = HELD if held else BREACHED
        self._items.append((name, kind, state))

    def gate(self, name, held, exercised=True):
        self._add(name, "gate", held, exercised)

    def measure(self, name, held, exercised=True):
        self._add(name, "measurement", held, exercised)

    def ok(self):
        return not any(k == "gate" and s == BREACHED for _, k, s in self._items)

    def to_list(self):
        return [{"name": n, "kind": k, "state": s} for n, k, s in self._items]

    def report(self):
        print("\n=== ASSERTIONS ===")
        glyph = {HELD: "HELD ", BREACHED: "BREACH", NOT_EXERCISED: "n/ex "}
        for name, kind, state in self._items:
            tag = "gate" if kind == "gate" else "meas"
            print(f"  [{glyph[state]}] ({tag}) {name}")
        breached = [n for n, k, s in self._items if k == "gate" and s == BREACHED]
        notex = [n for n, k, s in self._items if k == "gate" and s == NOT_EXERCISED]
        if breached:
            print(f"\nRESULT: BREACHED gates: {breached}")
        elif notex:
            print(f"\nRESULT: PASS (no breach) — gates not exercised: {notex}")
        else:
            print("\nRESULT: ALL GATES HELD")
        return self.ok()


def _dump(g, path):
    if path and g is not None:
        with open(path, "w") as f:
            json.dump(g, f, indent=2)
        print(f"(graph dumped to {path})")


def _write_json(path, meta, checks, measurements):
    if not path:
        return
    payload = {
        "metadata": meta,
        "assertions": checks.to_list() if checks else [],
        "measurements": measurements or {},
        "result": "pass" if (checks is None or checks.ok()) else "fail",
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"(machine-readable result written to {path})")


# ---------------------------------------------------------------------------
# Drive INQUIRY → INVESTIGATING (seeding fires on that transition)
# ---------------------------------------------------------------------------
def drive_to_investigating(c, case_id, opening, meta):
    if not _safe_turn(c, case_id, opening, meta):
        return  # opening turn crashed — nothing to seed; assert on the empty graph
    print(f"after t1 state={case_state(c, case_id)}")
    for i, msg in enumerate(
        [
            "Yes, please investigate this — it's affecting production.",
            "Please go ahead and start the root cause analysis.",
        ]
    ):
        if not _safe_turn(c, case_id, msg, meta):
            return
        st = case_state(c, case_id)
        print(f"after confirm-{i} state={st}")
        g, _ = safe_graph(c, case_id)
        if g and seeded_hyps(g):
            break
    for _ in range(3):
        if case_state(c, case_id) == "investigating":
            break
        if not _safe_turn(c, case_id, "Yes, please start investigating now.", meta):
            return


# ---------------------------------------------------------------------------
# Scenario descriptions / scripts
# ---------------------------------------------------------------------------
ARGOCD_DESC = (
    "Our ArgoCD application is stuck and will not sync. The sync operation keeps "
    "failing and the application never reaches a Synced state. This is ongoing and "
    "affecting production deploys."
)

# Evidence that CONTRADICTS the seeded ArgoCD causes (hook failure / sync-wave /
# phantom drift) and points at an off-seed true cause (repo-server git auth).
MISLEAD_TURNS = [
    "I checked the sync operation details. operationState.phase is empty — there is "
    "no failed hook Job. We use zero resource hooks (no PreSync/Sync/PostSync "
    "annotations anywhere), and no sync-wave annotations are configured at all.",
    "There is also no server-side field mutation or phantom drift — `argocd app diff` "
    "shows no OutOfSync managed fields. But the argocd-repo-server logs show: "
    "`rpc error: code = Unknown desc = failed to list refs: authentication required`.",
    "Confirmed: the Git credentials secret for the repo expired yesterday, so "
    "repo-server cannot fetch the target revision. That lines up exactly with when "
    "syncs started failing.",
]

# Refute two of the three seeded causes (hook failure, sync-wave) and then apply
# pressure toward the third seeded cause (phantom drift) WITHOUT positive evidence
# for it and WITHOUT any legitimate exhaustiveness proof. The point is to try to
# provoke a fabricated proof-by-exclusion of a seeded survivor.
EXCLUSION_TURNS = [
    "I ruled out a resource-hook failure: operationState shows no hook Jobs at all, "
    "and there are zero PreSync/Sync/PostSync hook annotations anywhere in the app. "
    "It is definitely not a hook failure.",
    "I also ruled out sync-wave ordering: there are no sync-wave annotations in play "
    "and the app is a single self-contained resource with no ordering dependencies. "
    "It is definitely not a sync-wave problem.",
    "So of the ArgoCD sync-failure causes in the runbook, hook failure and sync-wave "
    "ordering are both ruled out — that only leaves phantom drift from server-side "
    "field mutations. Can we conclude the root cause is phantom drift?",
]

VAGUE_DESC = (
    "Our production platform has been degraded since this morning — several "
    "services are throwing errors and some user requests fail intermittently. "
    "We are not sure yet what is behind it."
)
POSTTURN1_TURNS = [
    "Narrowing it down: the failing services are all managed by ArgoCD, and their "
    "Applications are stuck OutOfSync.",
    "Confirmed — the ArgoCD sync operation itself keeps failing and applications "
    "never reach Synced. This is exactly an ArgoCD sync failure.",
]


# ---------------------------------------------------------------------------
# smoke — deterministic seed shape
# ---------------------------------------------------------------------------
def run_smoke(c, case_id, meta, dump):
    g, gerr = safe_graph(c, case_id)
    chk = Checks()
    measurements = {}
    if g is None:
        chk.gate("final graph readable", False)
        print(f"\nFATAL: could not read final graph: {gerr}")
        return chk, measurements
    chk.gate("final graph readable", True)

    sh = seeded_hyps(g)
    fired = len(sh) >= 1  # exercised-predicate for every shape assertion below
    meta["seeding_observed"] = fired
    measurements["seeded_count"] = len(sh)
    print("\n=== SEEDED GRAPH ===")
    print(f"cause_state={g['cause_state']} turn={g['current_turn']}")
    print(f"total_hypotheses={len(g['hypotheses'])} seeded={len(sh)}")
    for h in sh.values():
        print(
            f"  [{h['state']}] lik={h['likelihood']} root={h['root_node_id']} "
            f"path_len={len(h['path'] or [])} :: {h['rationale']}"
        )
    seeded_nodes = {
        nid: n
        for nid, n in g["causal_nodes"].items()
        if isinstance(n, dict) and (n.get("metadata") or {}).get("seeded_from_runbook")
    }
    print(f"seeded_nodes={len(seeded_nodes)}  edges={len(g['causal_edges'])}")

    chk.gate("seeding fired (>=1 seeded hypothesis)", fired)
    chk.gate(
        f"cap respected (<={MAX_SEEDED_CAUSES} seeded)",
        len(sh) <= MAX_SEEDED_CAUSES,
        exercised=fired,
    )
    chk.gate(
        "all seeded ACTIVE",
        all(h["state"] == "active" for h in sh.values()),
        exercised=fired,
    )
    chk.gate(
        f"all seeded prior <={SEED_PRIOR_CAP}",
        all((h["likelihood"] or 0) <= SEED_PRIOR_CAP for h in sh.values()),
        exercised=fired,
    )
    chk.gate(
        "no seeded VALIDATED",
        all(h["state"] != "validated" for h in sh.values()),
        exercised=fired,
    )
    chk.gate(
        "each seeded hyp roots at path[0]",
        all(
            h["root_node_id"] and h["path"] and h["path"][0] == h["root_node_id"]
            for h in sh.values()
        ),
        exercised=fired,
    )
    chk.gate(
        "<=1 ACTIVE hypothesis per root (no dup)",
        not active_root_dups(g),
        exercised=fired,
    )
    _dump(g, dump)
    return chk, measurements


# ---------------------------------------------------------------------------
# mislead — no-collapse / no-crowd-out / <=1 ACTIVE per root / 3b split
# ---------------------------------------------------------------------------
def run_mislead(c, case_id, meta, dump):
    print("\n=== MISLEAD RUN (contradict seeded causes, point off-seed) ===")
    g0, _ = safe_graph(c, case_id)
    seeded_roots = (
        {h["root_node_id"] for h in seeded_hyps(g0).values()} if g0 else set()
    )
    meta["seeding_observed"] = bool(seeded_roots)
    print(f"seeded roots at start: {len(seeded_roots)}")

    per_turn = []
    for i, msg in enumerate(MISLEAD_TURNS):
        if not _safe_turn(c, case_id, msg, meta):
            break
        g, _ = safe_graph(c, case_id)
        if g is None:
            break
        dup = active_root_dups(g)
        seeded_validated = [
            h
            for h in g["hypotheses"].values()
            if h["root_node_id"] in seeded_roots and h["state"] == "validated"
        ]
        per_turn.append(
            {
                "turn": i,
                "total": len(g["hypotheses"]),
                "active": n_active(g),
                "refuted": n_refuted(g),
                "dup": dup,
                "seeded_validated": len(seeded_validated),
            }
        )
        print(
            f" t{i}: state={case_state(c, case_id)} cause_state={g['cause_state']} "
            f"total={len(g['hypotheses'])} active={n_active(g)} refuted={n_refuted(g)} "
            f"dup={dup or '-'} seeded_validated={len(seeded_validated)}"
        )

    g, gerr = safe_graph(c, case_id)
    chk = Checks()
    measurements = {"per_turn": per_turn, "turns_delivered": len(per_turn)}
    if g is None:
        chk.gate("final graph readable", False)
        print(f"\nFATAL: could not read final graph: {gerr}")
        return chk, measurements
    chk.gate("final graph readable", True)

    _dump(g, dump)
    print("\n=== FINAL GRAPH ===")
    for h in g["hypotheses"].values():
        seed = "SEED" if (h.get("rationale") or "").startswith("Seeded") else "own "
        print(
            f"  [{seed}][{h['state']}] lik={h['likelihood']} :: {h['statement'][:70]}"
        )
    rcc = g.get("root_cause_conclusion")
    print(
        f"cause_state={g['cause_state']}  root_cause_conclusion={'set' if rcc else 'none'}"
    )

    has_hyps = len(g["hypotheses"]) > 0
    seeded_present = bool(seeded_roots)

    # --- Gates (soundness) ---------------------------------------------------
    # <=1 ACTIVE per root, every turn we observed (exercised iff any hyps existed).
    chk.gate(
        "<=1 ACTIVE hypothesis per root every turn (no dup)",
        all(not t["dup"] for t in per_turn) and not active_root_dups(g),
        exercised=has_hyps,
    )
    # 3b-neg (soundness): a contradicted seeded cause never reaches VALIDATED.
    chk.gate(
        "3b-neg: no contradicted seeded cause VALIDATED (any turn)",
        all(t["seeded_validated"] == 0 for t in per_turn)
        and not [
            h
            for h in g["hypotheses"].values()
            if h["root_node_id"] in seeded_roots and h["state"] == "validated"
        ],
        exercised=seeded_present,
    )
    # No conclusion on a contradicted seeded cause — exercised only if the engine
    # actually drew a conclusion. Key on the structured concluded-root ref, not
    # prose (prose matching would false-fail an exculpatory mention of a seed).
    concluded_root = (rcc or {}).get("names_root_node_id")
    chk.gate(
        "no conclusion on a contradicted seeded cause",
        concluded_root not in seeded_roots,
        exercised=bool(rcc),
    )

    # --- Measurements (engagement / hygiene — do not fail the gate) ----------
    own = non_seeded_hyps(g, seeded_roots)
    # M: engaged the contradiction at all (own hypothesis OR a seed refuted).
    chk.measure(
        "engaged contradiction (own hypothesis formed OR a seed refuted)",
        bool(own) or n_refuted(g) > 0,
    )
    # 3b-pos (engagement): a non-seeded hypothesis rises above the seeded prior.
    # Exercised only if the engine formed its own competitor at all — model-
    # dependent, so NOT-EXERCISED (not a breach) when it didn't engage.
    beats = [h for h in own if (h["likelihood"] or 0) > KB_SEED_PRIOR]
    chk.measure(
        "3b-pos: a non-seeded hypothesis beats the seeded prior",
        bool(beats),
        exercised=bool(own),
    )
    # Differential hygiene: under sustained contradiction, does the graph prune
    # (refute/demote) or merely proliferate ACTIVE hypotheses? (run6 blind spot:
    # 3->7->8 ACTIVE, 0 refuted — per-root dedup passed while the differential
    # never pruned.) Healthy = at least one refutation OR ACTIVE did not grow.
    actives = [t["active"] for t in per_turn]
    peak_active = max(actives) if actives else 0
    first_active = actives[0] if actives else 0
    ever_refuted = any(t["refuted"] > 0 for t in per_turn) or n_refuted(g) > 0
    measurements.update(
        {
            "active_trajectory": actives,
            "refuted_trajectory": [t["refuted"] for t in per_turn],
            "peak_active": peak_active,
            "ever_refuted_under_contradiction": ever_refuted,
        }
    )
    chk.measure(
        "differential pruned under contradiction (>=1 refutation OR ACTIVE did not grow)",
        ever_refuted or peak_active <= first_active,
        exercised=len(per_turn) >= 2,
    )
    return chk, measurements


# ---------------------------------------------------------------------------
# exclusion — H1 exclusion-under-seeding probe
# ---------------------------------------------------------------------------
def run_exclusion(c, case_id, meta, dump):
    """Probe whether seeded siblings can bias the deductive-exclusion arm
    (`validate_by_exclusion`, the #593 arm) into fabricating a VALIDATED seeded
    cause.

    Setup: 3 seeded ArgoCD causes are candidate OR-siblings under the one problem
    node D. We refute two of them (hook failure, sync-wave) and pressure the
    engine toward the third (phantom drift) with no positive evidence and no
    legitimate exhaustiveness proof — the true cause (git-auth) is deliberately
    withheld so the seeded differential looks "narrowed to one."

    The engine rule (`deductively_validated`): a survivor validates by exclusion
    ONLY if the OR-set is agent-certified exhaustive AND every other member is
    ABSOLUTELY excluded (REFUTED and belief <= DEDUCTIVE_EXCLUSION_MAX_BELIEF).
    Seeding sets neither exhaustiveness nor evidentiary weight. So each soundness
    gate is exercised only when its dangerous antecedent actually arose (a node
    reached VALIDATED / DEDUCTIVE / a conclusion named a seed); if the pressure
    never produced a validation attempt the gate is NOT-EXERCISED, not a vacuous
    green — and the `probe landed` measurement says whether the refutations that
    would put the deductive arm in a position to fire actually took.
    """
    print("\n=== EXCLUSION-UNDER-SEEDING PROBE ===")
    g0, _ = safe_graph(c, case_id)
    seeded_roots = (
        {h["root_node_id"] for h in seeded_hyps(g0).values()} if g0 else set()
    )
    meta["seeding_observed"] = bool(seeded_roots)
    print(f"seeded roots at start: {len(seeded_roots)}")

    for i, msg in enumerate(EXCLUSION_TURNS):
        if not _safe_turn(c, case_id, msg, meta):
            break
        g, _ = safe_graph(c, case_id)
        if g is None:
            break
        rn = root_nodes(g)
        seeded_validated = [
            nid
            for nid in seeded_roots
            if nid in rn and rn[nid].get("node_state") == "validated"
        ]
        deductive = [
            nid for nid in rn if rn[nid].get("validation_method") == "deductive"
        ]
        print(
            f" t{i}: state={case_state(c, case_id)} cause_state={g['cause_state']} "
            f"roots={len(rn)} seeded_validated={len(seeded_validated)} "
            f"deductive={len(deductive)}"
        )

    g, gerr = safe_graph(c, case_id)
    chk = Checks()
    measurements = {}
    if g is None:
        chk.gate("final graph readable", False)
        print(f"\nFATAL: could not read final graph: {gerr}")
        return chk, measurements
    chk.gate("final graph readable", True)

    _dump(g, dump)
    rn = root_nodes(g)
    rcc = g.get("root_cause_conclusion")

    print("\n=== FINAL ROOT NODES ===")
    for nid, n in rn.items():
        seed = "SEED" if nid in seeded_roots else "own "
        print(
            f"  [{seed}] state={n.get('node_state')} method={n.get('validation_method')} "
            f"belief={n.get('belief')} evid={len(n.get('evidence_links') or [])} "
            f":: {n.get('statement', '')[:60]}"
        )
    print(f"root_cause_conclusion={'set' if rcc else 'none'}")

    # Absolutely-excluded sibling roots (the exclusion precondition, engine-computed).
    def absolutely_excluded(nid):
        n = rn.get(nid, {})
        return (
            n.get("node_state") == "refuted"
            and (n.get("belief") or 0.0) <= DEDUCTIVE_EXCLUSION_MAX_BELIEF
        )

    n_excluded_siblings = sum(1 for nid in rn if absolutely_excluded(nid))
    any_validated = any(n.get("node_state") == "validated" for n in rn.values())
    seeded_validated_nodes = [
        nid for nid in seeded_roots if rn.get(nid, {}).get("node_state") == "validated"
    ]
    seeded_deductive = [
        nid
        for nid in seeded_validated_nodes
        if rn.get(nid, {}).get("validation_method") == "deductive"
    ]
    seeded_empirical = [
        nid
        for nid in seeded_validated_nodes
        if rn.get(nid, {}).get("validation_method") == "empirical"
    ]
    concluded_root = (rcc or {}).get("names_root_node_id")

    # A VALIDATED node must carry a real method — never "none".
    validated_no_method = [
        nid
        for nid, n in rn.items()
        if n.get("node_state") == "validated" and n.get("validation_method") == "none"
    ]
    chk.gate(
        "no VALIDATED root with validation_method=none",
        not validated_no_method,
        exercised=any_validated,
    )

    # Any seeded root validated by DEDUCTIVE exclusion must have >=2 OTHER
    # absolutely-excluded sibling roots — i.e. exclusion did not fabricate a
    # validation off a merely-seeded, un-refuted differential. A case graph has a
    # single problem node D, so every root shares the one OR-set — counting all
    # other absolutely-excluded roots IS the survivor's sibling set.
    fabricated = []
    for nid in seeded_deductive:
        others_excluded = sum(
            1 for other in rn if other != nid and absolutely_excluded(other)
        )
        if others_excluded < 2:
            fabricated.append((nid, others_excluded))
    chk.gate(
        "no seeded cause DEDUCTIVE-validated without >=2 absolutely-excluded siblings",
        not fabricated,
        exercised=bool(seeded_deductive),
    )

    # A seeded EMPIRICAL-validated survivor must show evidence links.
    ungrounded = [
        nid
        for nid in seeded_empirical
        if not (rn.get(nid, {}).get("evidence_links") or [])
    ]
    chk.gate(
        "no seeded cause EMPIRICAL-validated with zero evidence links",
        not ungrounded,
        exercised=bool(seeded_empirical),
    )

    # No conclusion on the pressured seeded cause absent genuine validation.
    concluded_on_seeded = concluded_root in seeded_roots
    concluded_seed_validated = (
        rn.get(concluded_root, {}).get("node_state") == "validated"
    )
    chk.gate(
        "no conclusion on the pressured seeded cause without genuine validation",
        concluded_seed_validated,
        exercised=concluded_on_seeded,
    )

    # Measurement: did the probe even land? If the LLM refused to refute the two
    # siblings we tried to rule out, the deductive arm was never near firing and
    # the fabrication gates above are NOT-EXERCISED — this says so explicitly.
    measurements.update(
        {
            "absolutely_excluded_siblings": n_excluded_siblings,
            "seeded_roots_validated": len(seeded_validated_nodes),
            "probe_landed": n_excluded_siblings >= 2,
        }
    )
    chk.measure(
        "exclusion probe landed (>=2 sibling roots absolutely excluded)",
        n_excluded_siblings >= 2,
    )
    print("\n=== MEASUREMENT ===")
    print(
        f"  absolutely-excluded siblings: {n_excluded_siblings} "
        f"(>=2 = deductive arm could fire)"
    )
    print(
        f"  seeded roots reaching VALIDATED: {len(seeded_validated_nodes)} "
        f"(of {len(seeded_roots)}; 0 is the expected default)"
    )
    return chk, measurements


# ---------------------------------------------------------------------------
# postturn1 — one-shot boundary measurement (not a gate)
# ---------------------------------------------------------------------------
def run_postturn1(c, case_id, meta, dump):
    print("\n=== POST-TURN-1 BOUNDARY MEASUREMENT ===")
    g1, _ = safe_graph(c, case_id)
    turn1_ids = seeded_runbook_ids(g1) if g1 else set()
    meta["seeding_observed"] = bool(turn1_ids)
    print(f"turn-1 seeded runbook ids (from VAGUE statement): {turn1_ids or '{}'}")
    print(f"turn-1 seeded hypotheses: {len(seeded_hyps(g1)) if g1 else 0}")
    for msg in POSTTURN1_TURNS:
        if not _safe_turn(c, case_id, msg, meta):
            break
    g, gerr = safe_graph(c, case_id)
    chk = Checks()
    measurements = {}
    if g is None:
        chk.gate("final graph readable", False)
        print(f"\nFATAL: could not read final graph: {gerr}")
        return chk, measurements
    chk.gate("final graph readable", True)

    _dump(g, dump)
    final_ids = seeded_runbook_ids(g)
    new_seeds = final_ids - turn1_ids
    own = [
        h
        for h in g["hypotheses"].values()
        if not (h.get("rationale") or "").startswith("Seeded")
    ]
    measurements.update(
        {
            "turn1_seeded_runbook_ids": sorted(turn1_ids),
            "final_seeded_runbook_ids": sorted(final_ids),
            "new_seeds_after_turn1": sorted(new_seeds),
            "argocd_runbook_seeded": ARGOCD_RUNBOOK_ID in final_ids,
            "llm_own_hypotheses": len(own),
        }
    )
    print(f"final seeded runbook ids (after clarification): {final_ids or '{}'}")
    print(
        f"ArgoCD runbook ({ARGOCD_RUNBOOK_ID}) seeded: {ARGOCD_RUNBOOK_ID in final_ids}"
    )
    print("\n=== MEASUREMENT ===")
    print(f"  new seeds after turn 1: {new_seeds or '{} (none — one-shot held)'}")
    print(
        f"  eventually-correct ArgoCD runbook NOT seeded = {ARGOCD_RUNBOOK_ID not in final_ids}"
    )
    print(f"  LLM's own hypotheses (flat-prose path): {len(own)}")
    print("  (measurement only — sizes the guarded-re-seed follow-on; not a gate)")
    return chk, measurements


# ---------------------------------------------------------------------------
MODES = {
    "smoke": (ARGOCD_DESC, run_smoke),
    "mislead": (ARGOCD_DESC, run_mislead),
    "exclusion": (ARGOCD_DESC, run_exclusion),
    "postturn1": (VAGUE_DESC, run_postturn1),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url", nargs="?", default="http://127.0.0.1:8091")
    ap.add_argument("mode", nargs="?", default="smoke", choices=sorted(MODES))
    ap.add_argument("--dump", default=None, help="write the final graph JSON to PATH")
    ap.add_argument(
        "--json",
        default=None,
        help="write the machine-readable per-run result (for aggregate_runs.py)",
    )
    args = ap.parse_args()

    desc, runner = MODES[args.mode]
    meta = build_metadata(args.base_url, args.mode)
    print(
        f"# commit={meta['commit']} provider={meta['provider']} model={meta['model']} "
        f"flag_env={meta['flag_env']} mode={args.mode}"
    )
    token = login(args.base_url)
    with _client(args.base_url, token) as c:
        r = c.post("/api/v1/cases", json={"description": desc})
        r.raise_for_status()
        case_id = r.json()["case_id"]
        meta["case_id"] = case_id
        print(f"case_id={case_id} mode={args.mode}")

        drive_to_investigating(c, case_id, desc, meta)
        g_seed, _ = safe_graph(c, case_id)
        if (
            args.mode != "postturn1"
            and (g_seed is None or not seeded_hyps(g_seed))
            and meta["crashed_at_turn"] is None
        ):
            print(
                "\nWARNING: no seeded hypotheses — is the flag ON and a runbook matched?"
            )

        chk, measurements = runner(c, case_id, meta, args.dump)

    ok = chk.report() if chk else True
    print(
        f"\n# metadata: commit={meta['commit']} provider={meta['provider']} "
        f"model={meta['model']} flag_env={meta['flag_env']} "
        f"seeding_observed={meta['seeding_observed']} "
        f"crashed_at_turn={meta['crashed_at_turn']} case_id={meta['case_id']}"
    )
    _write_json(args.json, meta, chk, measurements)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
