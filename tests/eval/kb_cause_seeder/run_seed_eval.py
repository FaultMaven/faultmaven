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

Usage:
    python run_seed_eval.py <base_url> <mode> [--dump PATH]

Modes:
    smoke      seed + shape assertions (deterministic; provider-independent)
    mislead    contradict the seeded causes, point off-seed → no-collapse,
               no-crowd-out, <=1 ACTIVE/root, prior-not-gate (3b)
    exclusion  refute all-but-one seeded sibling and pressure toward the last
               one → the deductive-exclusion arm never fabricates a VALIDATED
               seeded cause (H1 exclusion-under-seeding probe)
    postturn1  vague-open → clarify: measures the one-shot seeding boundary

Exit code is 0 iff every assertion passed (measurement-only modes exit 0).
"""

import argparse
import json
import sys
from collections import Counter

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


def case_state(c, case_id):
    r = c.get(f"/api/v1/cases/{case_id}")
    r.raise_for_status()
    d = r.json()
    return d.get("state") or d.get("status") or d.get("case_status")


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


class Checks:
    def __init__(self):
        self._items = []

    def add(self, name, cond):
        self._items.append((name, bool(cond)))

    def report(self):
        print("\n=== ASSERTIONS ===")
        for name, ok in self._items:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        failed = [n for n, ok in self._items if not ok]
        print(f"\nRESULT: {'ALL PASS' if not failed else 'FAILURES: ' + str(failed)}")
        return not failed


def _dump(g, path):
    if path:
        with open(path, "w") as f:
            json.dump(g, f, indent=2)
        print(f"(graph dumped to {path})")


# ---------------------------------------------------------------------------
# Drive INQUIRY → INVESTIGATING (seeding fires on that transition)
# ---------------------------------------------------------------------------
def drive_to_investigating(c, case_id, opening):
    turn(c, case_id, opening)
    print(f"after t1 state={case_state(c, case_id)}")
    for i, msg in enumerate(
        [
            "Yes, please investigate this — it's affecting production.",
            "Please go ahead and start the root cause analysis.",
        ]
    ):
        turn(c, case_id, msg)
        st = case_state(c, case_id)
        print(f"after confirm-{i} state={st}")
        if seeded_hyps(graph(c, case_id)):
            break
    for _ in range(3):
        if case_state(c, case_id) == "investigating":
            break
        turn(c, case_id, "Yes, please start investigating now.")


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
def run_smoke(c, case_id, dump):
    g = graph(c, case_id)
    sh = seeded_hyps(g)
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

    chk = Checks()
    chk.add("seeding fired (>=1 seeded hypothesis)", len(sh) >= 1)
    chk.add(
        f"cap respected (<={MAX_SEEDED_CAUSES} seeded)", len(sh) <= MAX_SEEDED_CAUSES
    )
    chk.add("all seeded ACTIVE", all(h["state"] == "active" for h in sh.values()))
    chk.add(
        f"all seeded prior <={SEED_PRIOR_CAP}",
        all((h["likelihood"] or 0) <= SEED_PRIOR_CAP for h in sh.values()),
    )
    chk.add("no seeded VALIDATED", all(h["state"] != "validated" for h in sh.values()))
    chk.add(
        "each seeded hyp roots at path[0]",
        all(
            h["root_node_id"] and h["path"] and h["path"][0] == h["root_node_id"]
            for h in sh.values()
        ),
    )
    chk.add("<=1 ACTIVE hypothesis per root (no dup)", not active_root_dups(g))
    _dump(g, dump)
    return chk.report()


# ---------------------------------------------------------------------------
# mislead — no-collapse / no-crowd-out / <=1 ACTIVE per root / prior-not-gate
# ---------------------------------------------------------------------------
def run_mislead(c, case_id, dump):
    print("\n=== MISLEAD RUN (contradict seeded causes, point off-seed) ===")
    g0 = graph(c, case_id)
    seeded_roots = {h["root_node_id"] for h in seeded_hyps(g0).values()}
    print(f"seeded roots at start: {len(seeded_roots)}")

    per_turn = []
    for i, msg in enumerate(MISLEAD_TURNS):
        turn(c, case_id, msg)
        g = graph(c, case_id)
        dup = active_root_dups(g)
        seeded_validated = [
            h
            for h in g["hypotheses"].values()
            if h["root_node_id"] in seeded_roots and h["state"] == "validated"
        ]
        n_active = sum(1 for h in g["hypotheses"].values() if h["state"] == "active")
        n_refuted = sum(1 for h in g["hypotheses"].values() if h["state"] == "refuted")
        per_turn.append(
            {
                "turn": i,
                "total": len(g["hypotheses"]),
                "active": n_active,
                "refuted": n_refuted,
                "dup": dup,
                "seeded_validated": len(seeded_validated),
            }
        )
        print(
            f" t{i}: state={case_state(c, case_id)} cause_state={g['cause_state']} "
            f"total={len(g['hypotheses'])} active={n_active} refuted={n_refuted} "
            f"dup={dup or '-'} seeded_validated={len(seeded_validated)}"
        )

    g = graph(c, case_id)
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

    final = per_turn[-1]
    chk = Checks()
    chk.add(
        "<=1 ACTIVE hypothesis per root every turn (no dup)",
        all(not t["dup"] for t in per_turn),
    )
    chk.add(
        "no contradicted seeded cause VALIDATED (any turn)",
        all(t["seeded_validated"] == 0 for t in per_turn),
    )
    chk.add(
        "engaged contradiction (own hypothesis formed OR a seed refuted)",
        (final["total"] > len(seeded_roots)) or (final["refuted"] > 0),
    )
    # The conclusion (if any) must not name a seeded cause's root. Key on the
    # engine's structured concluded-root reference, not the model's prose — prose
    # matching would false-fail an exculpatory mention of a seeded cause name.
    concluded_root = (rcc or {}).get("names_root_node_id")
    chk.add(
        "no conclusion on a contradicted seeded cause",
        (not rcc) or (concluded_root not in seeded_roots),
    )
    # 3b prior-not-gate: a non-seeded hypothesis beats the seeded prior; no seed VALIDATED.
    non_seeded_beats = [
        h
        for h in g["hypotheses"].values()
        if h["root_node_id"]
        and h["root_node_id"] not in seeded_roots
        and (h["likelihood"] or 0) > KB_SEED_PRIOR
    ]
    seeded_validated_any = [
        h
        for h in g["hypotheses"].values()
        if h["root_node_id"] in seeded_roots and h["state"] == "validated"
    ]
    chk.add(
        "3b: a non-seeded hypothesis beats the seeded prior; no seed VALIDATED",
        bool(non_seeded_beats) and not seeded_validated_any,
    )
    return chk.report()


# ---------------------------------------------------------------------------
# exclusion — H1 exclusion-under-seeding probe
# ---------------------------------------------------------------------------
def run_exclusion(c, case_id, dump):
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
    Seeding sets neither exhaustiveness nor evidentiary weight. So the invariant,
    LLM-agnostic:

      Any seeded root that is VALIDATED must carry a real validation_method, and
      if that method is DEDUCTIVE its exclusion precondition must genuinely hold
      (>=2 sibling roots REFUTED at belief <= 0.05). Seeding never lowers the bar.
    """
    print("\n=== EXCLUSION-UNDER-SEEDING PROBE ===")
    g0 = graph(c, case_id)
    seeded_roots = {h["root_node_id"] for h in seeded_hyps(g0).values()}
    print(f"seeded roots at start: {len(seeded_roots)}")

    for i, msg in enumerate(EXCLUSION_TURNS):
        turn(c, case_id, msg)
        g = graph(c, case_id)
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

    g = graph(c, case_id)
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

    chk = Checks()

    # A VALIDATED node must carry a real method — never "none".
    validated_no_method = [
        nid
        for nid, n in rn.items()
        if n.get("node_state") == "validated" and n.get("validation_method") == "none"
    ]
    chk.add("no VALIDATED root with validation_method=none", not validated_no_method)

    # The headline: any seeded root validated by DEDUCTIVE exclusion must have >=2
    # OTHER absolutely-excluded sibling roots — i.e. exclusion did not fabricate a
    # validation off a merely-seeded, un-refuted differential. A case graph has a
    # single problem node D (one per case), so every root shares the one OR-set —
    # counting all other roots IS the survivor's sibling set.
    fabricated = []
    for nid in seeded_roots:
        n = rn.get(nid)
        if not n or n.get("node_state") != "validated":
            continue
        if n.get("validation_method") == "deductive":
            others_excluded = sum(
                1 for other in rn if other != nid and absolutely_excluded(other)
            )
            if others_excluded < 2:
                fabricated.append((nid, others_excluded))
    chk.add(
        "no seeded cause DEDUCTIVE-validated without >=2 absolutely-excluded siblings",
        not fabricated,
    )

    # A seeded cause that reaches VALIDATED at all must show real grounding —
    # either DEDUCTIVE (checked above) or EMPIRICAL with evidence links. A seeded
    # survivor with zero evidence and no genuine exclusion must NOT be validated.
    ungrounded_validated = []
    for nid in seeded_roots:
        n = rn.get(nid)
        if not n or n.get("node_state") != "validated":
            continue
        method = n.get("validation_method")
        if method == "empirical" and not (n.get("evidence_links") or []):
            ungrounded_validated.append((nid, "empirical-no-evidence"))
    chk.add(
        "no seeded cause EMPIRICAL-validated with zero evidence links",
        not ungrounded_validated,
    )

    # No conclusion on a seeded cause absent genuine validation. Key on the
    # engine's structured concluded-root reference, not the model's prose — a
    # conclusion that names a seeded root is a collapse unless that very root is
    # genuinely VALIDATED (method != none, and if DEDUCTIVE the precondition
    # above held).
    concluded_root = (rcc or {}).get("names_root_node_id")
    concluded_on_seeded_ungrounded = (
        concluded_root in seeded_roots
        and rn.get(concluded_root, {}).get("node_state") != "validated"
    )
    chk.add(
        "no conclusion on the pressured seeded cause without genuine validation",
        not concluded_on_seeded_ungrounded,
    )

    # Measurement (not a gate): did exclusion fire at all on a seeded survivor?
    n_seeded_validated = sum(
        1 for nid in seeded_roots if rn.get(nid, {}).get("node_state") == "validated"
    )
    print("\n=== MEASUREMENT ===")
    print(
        f"  seeded roots reaching VALIDATED: {n_seeded_validated} (of {len(seeded_roots)})"
    )
    print(
        "  (0 is the expected default — the seeded differential is not agent-certified "
        "exhaustive, and the true cause is off-seed; any VALIDATED here is checked to "
        "have met the genuine exclusion/empirical preconditions above)"
    )
    return chk.report()


# ---------------------------------------------------------------------------
# postturn1 — one-shot boundary measurement (not a gate)
# ---------------------------------------------------------------------------
def run_postturn1(c, case_id, dump):
    print("\n=== POST-TURN-1 BOUNDARY MEASUREMENT ===")
    g1 = graph(c, case_id)
    turn1_ids = seeded_runbook_ids(g1)
    print(f"turn-1 seeded runbook ids (from VAGUE statement): {turn1_ids or '{}'}")
    print(f"turn-1 seeded hypotheses: {len(seeded_hyps(g1))}")
    for msg in POSTTURN1_TURNS:
        turn(c, case_id, msg)
    g = graph(c, case_id)
    _dump(g, dump)
    final_ids = seeded_runbook_ids(g)
    print(f"final seeded runbook ids (after clarification): {final_ids or '{}'}")
    print(
        f"ArgoCD runbook ({ARGOCD_RUNBOOK_ID}) seeded: {ARGOCD_RUNBOOK_ID in final_ids}"
    )
    own = [
        h
        for h in g["hypotheses"].values()
        if not (h.get("rationale") or "").startswith("Seeded")
    ]
    print("\n=== MEASUREMENT ===")
    print(
        f"  new seeds after turn 1: {final_ids - turn1_ids or '{} (none — one-shot held)'}"
    )
    print(
        f"  eventually-correct ArgoCD runbook NOT seeded = {ARGOCD_RUNBOOK_ID not in final_ids}"
    )
    print(f"  LLM's own hypotheses (flat-prose path): {len(own)}")
    print("  (measurement only — sizes the guarded-re-seed follow-on; not a gate)")
    return True


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
    args = ap.parse_args()

    desc, runner = MODES[args.mode]
    token = login(args.base_url)
    with _client(args.base_url, token) as c:
        r = c.post("/api/v1/cases", json={"description": desc})
        r.raise_for_status()
        case_id = r.json()["case_id"]
        print(f"case_id={case_id} mode={args.mode}")

        drive_to_investigating(c, case_id, desc)
        if not seeded_hyps(graph(c, case_id)) and args.mode != "postturn1":
            print(
                "\nWARNING: no seeded hypotheses — is the flag ON and a runbook matched?"
            )

        ok = runner(c, case_id, args.dump)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
