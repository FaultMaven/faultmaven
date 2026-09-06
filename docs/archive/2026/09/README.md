# Archived planning documents

These are **completed** planning documents, moved here from `docs/working/`
(which is deliberately untracked — see `.gitignore`, "local working files").
They are kept because they record *why* a thing was built the way it was, and
that reasoning outlives the plan.

**They describe the past.** A statement here was true when written and may not
be now. For current behaviour read the code, `CLAUDE.md`, or the ADRs in
`faultmaven-doc-internal`. Anything still to be done lives in an issue, not
here — the table below says where.

| Folder | What it planned | Residue |
|---|---|---|
| `rcc-dual-authoring/` | Read-time assurance-grade labeling on cause surfaces, and the dual-authoring gap analysis behind it | §3.2, §3.3 and §3.5 shipped. §3.1 — derive the conclusion from the validated chain — is **fm#673**, still open; this design is its record |
| `release-blocker-campaign/` | Rounds 1–8 of the pre-beta blocker campaign, plus the round-8 handover and the 2026-08-02 board snapshot | None. Open issues cited are the backlog it tracked, not unfinished plan. The handover's "traps already paid for" section is the reason to keep it |
| `owner-decisions/` | Dated owner decision briefs, 2026-07-29 through 2026-09-05 | All decisions resolved or lifted: **fm#1346** (api latency threshold), **fm#1335** (monitoring docs), **fm#1252** (live tenant assertion), **fm#1116**/**fm#1114** (reasoning path). fm#1293 and fm#1295 closed |
| `kb/` | KB remediation plan; runbook process-fit proposal | None. The runbook role question was decided by A/B and closed as **fm#1295** |
| `org-model/` | Org-model alignment; the U13 org/team management console design | Superseded by ADR-017 (enterprise is the isolation boundary), whose plan is still in `docs/working/` |
| `engine/` | Two-case engine convergence analysis; the #673 stage-A task spec | Both feed **fm#673** and **fm#1142** |
| `reviews/` | PR #993 observation-time provenance review; audit plan refinement | None |
| `cutover/` | The #629 flip rehearsal plan | Tracked by **fm#819** |
| `brand-sync/` (in `2026/08/`) | Two brand/terminology sync passes across the repos | None |

## Why a plan can be archived with an issue still open

The test is not "are all referenced issues closed". A campaign document cites
the whole backlog it was steering. The test is whether anything in the document
exists *only* in the document — if the remaining work is carried by an issue,
the plan is done being a plan.

One case here was found the hard way: a design correctly excluded a surface
because "its structured tabs render a milestone boolean", and the milestone had
been retired the same day. The exclusion was sound; its premise had expired. So
when archiving, check the plan's *assumptions* still hold, not only that its
tasks shipped.
