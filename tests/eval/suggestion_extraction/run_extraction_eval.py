#!/usr/bin/env python3
"""#1226 extraction measurement — does an approved suggestion actually publish?

The re-runnable artifact behind the claim this lane exists to make. Since #1214
``upload_document`` enforces ``RunbookValidator`` before its first side effect,
so a suggestion approved without a human edit publishes only if the extractor's
output CLEARS that gate. The pre-#1226 extraction prompt asked for
``## Problem / ## Root Cause / ## Solution / ## Prevention``, which fails it with
six errors — no frontmatter, five of the six required sections absent — so
one-click approval did not exist. This driver measures whether it does now.

**It measures a prompt against a live model, so it needs a provider key.** That
is the point: a fixture-only run proves the plumbing, not the prompt. Both
halves are runnable —

    python tests/eval/suggestion_extraction/run_extraction_eval.py before
    python tests/eval/suggestion_extraction/run_extraction_eval.py after
    python tests/eval/suggestion_extraction/run_extraction_eval.py both --json out.json

— and ``replay`` re-scores a recorded run offline, with no key and no network:

    python tests/eval/suggestion_extraction/run_extraction_eval.py replay \\
        --from tests/eval/suggestion_extraction/recorded-runs/<file>.json

Not a CI test. Its numbers are facts about one model on one fixture set, not
invariants; the invariants it motivated are pinned in
``tests/unit/modules/knowledge/test_extraction_emits_v4_schema_1226.py``.

What each mode does
-------------------
``before``  Replays the PRE-#1226 path verbatim: the old single prompt, the old
            2000-token cap, one attempt, no repair, no frontmatter-id forcing.
            The constant below is that prompt copied from the commit before this
            lane, so the baseline is the code that shipped rather than a
            paraphrase of it.
``after``   Drives the REAL ``SuggestionService.extract_knowledge_from_case``
            over a stub case repository. Nothing about the prompt, the retry, or
            the id minting is re-implemented here — a driver that re-implements
            the path it is measuring measures itself.
``both``    Runs both against the same fixtures in one process, so the two
            numbers come from the same model on the same day.
``replay``  Re-scores the runbooks recorded by an earlier ``--json`` run.

Reading the output: the headline is the pass rate of ``validate_content`` over
the case corpus. ``cases.json`` deliberately includes one THIN case (an
under-specified "login is slow" with no evidence) where there is no failure mode
to write a runbook about — a draft that fails there is the honest outcome, not a
regression, so the summary reports it apart from the rest.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

HERE = Path(__file__).resolve().parent
DEFAULT_CASES = HERE / "cases.json"

# The pre-#1226 prompt, copied verbatim from
# faultmaven/modules/knowledge/domain/services/suggestion_service.py at
# d8b8378a3f839e205268edad031989d2bcab40ea. Kept here so `before` measures the
# code that actually shipped; do not tidy it.
BASELINE_EXTRACTION_PROMPT = """Analyze this incident case and extract reusable knowledge.

Case Title: {case_title}
Description: {case_description}

{messages_section}

{evidence_section}

Generate a knowledge article that:
1. Describes the problem pattern (symptoms, scope, conditions)
2. Explains the root cause
3. Provides step-by-step resolution
4. Includes prevention recommendations
5. **CRITICAL**: Remove ALL incident-specific details:
   - Specific timestamps (use relative time like "after X hours")
   - Specific user names or email addresses
   - Specific hostnames, IP addresses, or internal URLs
   - Specific customer or organization names
   - Any other personally identifiable information

Format as Markdown with these sections:
## Problem
## Root Cause
## Solution
## Prevention
"""

# The pre-#1226 output cap on that call.
BASELINE_MAX_TOKENS = 2000

# The one case with nothing to extract. Reported apart from the rest: a failing
# draft there is the correct outcome, and folding it into the headline would
# either flatter or punish the prompt for the wrong reason.
THIN_CASE_IDS = {"case_ev8_thin_case"}


# ---------------------------------------------------------------------------
# Fixtures -> the objects SuggestionService reads off a Case
# ---------------------------------------------------------------------------


def load_cases(path: Path) -> list[dict]:
    return json.loads(path.read_text())["cases"]


class StubCaseRepository:
    """The three reads ``extract_knowledge_from_case`` makes, and nothing else."""

    def __init__(self, case: dict):
        self._case = case

    async def get_by_id(self, case_id: str):
        return SimpleNamespace(
            title=self._case["title"], description=self._case["description"]
        )

    async def get_messages(self, case_id: str):
        return [SimpleNamespace(**m) for m in self._case.get("messages", [])]

    async def get_evidence(self, case_id: str):
        return [SimpleNamespace(**e) for e in self._case.get("evidence", [])]


class CountingProvider:
    """Wraps the router and counts generation calls, so a run reports how many
    attempts the retry budget actually spent rather than assuming."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0

    async def generate(self, **kwargs):
        self.calls += 1
        return await self._inner.generate(**kwargs)


def _resolved_model() -> str:
    """The model the router will actually use, asked of settings rather than
    guessed from whichever ``*_MODEL`` env var happens to be set."""
    try:
        from faultmaven.config.settings import get_settings

        return get_settings().llm.get_knowledge_model() or "(provider default)"
    except Exception:  # a recorded run should never fail on its own metadata
        return "(unresolved)"


def build_provider():
    from faultmaven.container.providers.infrastructure import create_llm_provider

    return create_llm_provider()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score(content: str) -> dict:
    from faultmaven.modules.knowledge.domain.services.runbook_validator import (
        RunbookValidator,
    )

    result = RunbookValidator().validate_content(content)
    return {
        "passed": result.passed,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
    }


# ---------------------------------------------------------------------------
# The two paths
# ---------------------------------------------------------------------------


async def run_before(cases: list[dict], provider) -> list[dict]:
    rows = []
    for case in cases:
        messages_section = ""
        if case.get("messages"):
            messages_section = "Messages:\n" + "\n".join(
                f"[{m['role']}]: {m['content']}" for m in case["messages"][:50]
            )
        evidence_section = ""
        if case.get("evidence"):
            evidence_section = "Evidence Summary:\n" + "\n".join(
                f"- [{e['artifact_type']}] {e['name']}: {e['summary']}"
                for e in case["evidence"][:20]
            )
        prompt = BASELINE_EXTRACTION_PROMPT.format(
            case_title=case["title"],
            case_description=case["description"],
            messages_section=messages_section or "No messages included.",
            evidence_section=evidence_section or "No evidence included.",
        )
        started = time.time()
        try:
            response = await provider.generate(
                prompt=prompt, max_tokens=BASELINE_MAX_TOKENS, temperature=0.3
            )
            content = (getattr(response, "content", "") or "").strip()
            error = None
        except Exception as e:  # a provider fault is data, not a crash
            content, error = "", f"{type(e).__name__}: {e}"
        row = {
            "case_id": case["case_id"],
            "arm": "before",
            "attempts": 1,
            "seconds": round(time.time() - started, 1),
            "provider_error": error,
            "content": content,
        }
        row.update(score(content))
        rows.append(row)
        _echo(row)
    return rows


async def run_after(
    cases: list[dict], provider, attempts: int | None = None
) -> list[dict]:
    from faultmaven.modules.knowledge.domain.services.suggestion_service import (
        SuggestionService,
    )

    rows = []
    for case in cases:
        counting = CountingProvider(provider)
        service = SuggestionService(
            case_repository=StubCaseRepository(case),
            knowledge_service=None,
            sanitizer=None,
            llm_provider=counting,
            max_extraction_attempts=attempts,
        )
        started = time.time()
        error = None
        try:
            suggestion = await service.extract_knowledge_from_case(
                case_id=case["case_id"],
                organization_id="org-eval",
                extracted_by="eval-driver",
            )
            content = suggestion.suggested_content
        except Exception as e:
            content, error = "", f"{type(e).__name__}: {e}"
        row = {
            "case_id": case["case_id"],
            "arm": "after",
            "attempts": counting.calls,
            "seconds": round(time.time() - started, 1),
            "provider_error": error,
            "content": content,
        }
        row.update(score(content))
        rows.append(row)
        _echo(row)
    return rows


def _echo(row: dict) -> None:
    mark = "PASS" if row["passed"] else "FAIL"
    print(
        f"  [{mark}] {row['case_id']:<24} attempts={row['attempts']} "
        f"{row['seconds']:>5}s errors={len(row['errors'])}"
    )
    if not row["passed"]:
        for e in row["errors"][:6]:
            print(f"           - {e}")
        if row["provider_error"]:
            print(f"           ! provider: {row['provider_error']}")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def summarise(rows: list[dict]) -> dict:
    substantive = [r for r in rows if r["case_id"] not in THIN_CASE_IDS]
    thin = [r for r in rows if r["case_id"] in THIN_CASE_IDS]
    return {
        "arm": rows[0]["arm"] if rows else "?",
        "substantive_pass": sum(1 for r in substantive if r["passed"]),
        "substantive_total": len(substantive),
        "thin_pass": sum(1 for r in thin if r["passed"]),
        "thin_total": len(thin),
        "total_attempts": sum(r["attempts"] for r in rows),
        "passed_on_attempt": {
            str(n): sum(1 for r in rows if r["passed"] and r["attempts"] == n)
            for n in sorted({r["attempts"] for r in rows if r["passed"]})
        },
    }


def print_summary(summary: dict) -> None:
    n, d = summary["substantive_pass"], summary["substantive_total"]
    pct = (100.0 * n / d) if d else 0.0
    print(
        f"\n{summary['arm'].upper():<7} pass rate {n}/{d} ({pct:.0f}%) "
        f"on substantive cases; thin case {summary['thin_pass']}/"
        f"{summary['thin_total']}; {summary['total_attempts']} LLM call(s) total"
    )
    if summary["passed_on_attempt"]:
        detail = ", ".join(
            f"attempt {k}: {v}" for k, v in summary["passed_on_attempt"].items()
        )
        print(f"        passes by attempt count — {detail}")


# ---------------------------------------------------------------------------


async def main_async(args) -> int:
    cases = load_cases(Path(args.cases))
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["case_id"] in wanted]
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2

    if args.mode == "replay":
        recorded = json.loads(Path(args.from_file).read_text())
        for arm_rows in recorded["arms"].values():
            rescored = []
            for row in arm_rows:
                fresh = dict(row)
                fresh.update(score(row["content"]))
                _echo(fresh)
                rescored.append(fresh)
            print_summary(summarise(rescored))
        return 0

    provider = build_provider()
    arms: dict[str, list[dict]] = {}
    if args.mode in ("before", "both"):
        print(f"\n--- BEFORE (pre-#1226 prompt, {len(cases)} cases) ---")
        arms["before"] = await run_before(cases, provider)
        print_summary(summarise(arms["before"]))
    if args.mode in ("after", "both"):
        print(f"\n--- AFTER (shipped extraction path, {len(cases)} cases) ---")
        arms["after"] = await run_after(cases, provider, args.attempts)
        print_summary(summarise(arms["after"]))

    if args.json:
        payload = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "model": _resolved_model(),
            "chat_provider": os.environ.get("CHAT_PROVIDER", "(unset)"),
            "cases_file": str(args.cases),
            "arms": arms,
            "summaries": {k: summarise(v) for k, v in arms.items()},
        }
        Path(args.json).write_text(json.dumps(payload, indent=2))
        print(f"\nrecorded -> {args.json}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["before", "after", "both", "replay"])
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    ap.add_argument(
        "--provider",
        help=(
            "CHAT_PROVIDER for this run, applied before settings are read. The "
            "shipped default (gemini) answered 503 'high demand' for the whole "
            "of the recorded window, so the committed run names anthropic here "
            "rather than silently measuring a saturated endpoint."
        ),
    )
    ap.add_argument("--only", help="comma-separated case_ids")
    ap.add_argument(
        "--attempts",
        type=int,
        help=(
            "Override MAX_EXTRACTION_ATTEMPTS for the `after` arm. `--attempts "
            "1` is how the FIRST-DRAFT error profile is read: the surfaced "
            "validator errors are what the repair turn would otherwise have "
            "been handed, and reading them is how a systematic prompt defect "
            "is told apart from model noise."
        ),
    )
    ap.add_argument("--json", help="write the full run (runbooks included) here")
    ap.add_argument("--from", dest="from_file", help="recorded run to replay")
    args = ap.parse_args()
    if args.mode == "replay" and not args.from_file:
        ap.error("replay needs --from")
    if args.provider:
        # Before any settings read: LLMSettings resolves CHAT_PROVIDER from the
        # environment, and an env var set after the first get_settings() call
        # is ignored by the cached instance.
        os.environ["CHAT_PROVIDER"] = args.provider
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
