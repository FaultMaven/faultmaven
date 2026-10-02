"""Measure which control bounds thinking on Claude Opus 5, Opus 5.5 and Fable 5.1 (#1800).

Each call replays a REAL FaultMaven INVESTIGATING request, captured offline
from the API (no network) for that model, with one thinking control applied:

* ``omitted``    the body exactly as FaultMaven sends it for ``ANTHROPIC_THINKING_MODE=off``;
* ``effort_low`` the same body plus ``output_config: {"effort": "low"}``;
* ``disabled``   the same body plus ``thinking: {"type": "disabled"}``.

The live calls cost money, so the plan is fixed: ``APPROVED_PLAN`` is the set the
owner approved on #1800 (2026-09-30). That approval is SPENT: the seven calls
were sent on 2026-10-02. A new run needs a new owner approval.

Without ``--send`` the script prints the plan and sends nothing. With ``--send``
it reads ``--out`` first and skips every (model, variant) pair that already has
a row there, success or failure, because a transport failure may still have
been billed. Every other call is sent once, in plan order, and never retried.

Before anything is sent, the script exits 2 on any of these:
* a ``--request`` for a model outside the plan, or a model given twice;
* a planned body with ``max_tokens`` over 8000, a ``stream`` key, a server
  tool (no ``input_schema``) or more than 250,000 characters;
* an API key containing whitespace or a control character.

    python scripts/anthropic_thinking_controls.py \
        --request claude-opus-5=req-opus5.json --request claude-opus-5-5=req-opus55.json \
        --request claude-fable-5-1=req-fable51.json --out results.jsonl [--send]

Each response's status and JSON is appended to ``<out>.raw.jsonl`` before it is
summarised into ``--out``. The API key is read from ``ANTHROPIC_API_KEY``. It is
never printed, and every record is redacted of it before it is printed or written.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

#: (model, variant) pairs the owner approved on #1800. Order is run order.
APPROVED_PLAN: tuple[tuple[str, str], ...] = (
    ("claude-opus-5", "omitted"),
    ("claude-opus-5", "effort_low"),
    ("claude-opus-5", "disabled"),
    ("claude-opus-5-5", "omitted"),
    ("claude-opus-5-5", "effort_low"),
    ("claude-fable-5-1", "omitted"),
    ("claude-fable-5-1", "effort_low"),
)

VARIANTS = ("omitted", "effort_low", "disabled")

#: Bounds of the approved requests. A planned body outside them is refused.
MAX_TOKENS = 8000
MAX_BODY_CHARS = 250_000

REDACTED = "<redacted>"


def build_variant(body: dict[str, Any], model: str, variant: str) -> dict[str, Any]:
    """The captured body for ``model`` with one thinking control applied.

    The captured body is never mutated. ``omitted`` strips any ``thinking`` and
    ``output_config.effort`` the capture carried, so the variant is exactly the
    control it names.
    """
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    if body.get("model") != model:
        raise ValueError(f"captured body is for {body.get('model')!r}, not {model!r}")
    out = copy.deepcopy(body)
    out.pop("thinking", None)
    output_config = dict(out.pop("output_config", None) or {})
    output_config.pop("effort", None)
    if variant == "effort_low":
        output_config["effort"] = "low"
    elif variant == "disabled":
        out["thinking"] = {"type": "disabled"}
    if output_config:
        out["output_config"] = output_config
    return out


def summarize_response(
    status: int, payload: dict[str, Any], elapsed: float
) -> dict[str, Any]:
    """One result row from an HTTP status and the decoded JSON body."""
    row: dict[str, Any] = {"http": status, "elapsed_s": round(elapsed, 1)}
    if status != 200:
        err = payload.get("error") if isinstance(payload, dict) else None
        row["error"] = (
            (err or {}).get("message") if isinstance(err, dict) else str(payload)[:300]
        )
        return row
    usage = payload.get("usage") or {}
    details = usage.get("output_tokens_details") or {}
    output_tokens = usage.get("output_tokens")
    thinking_tokens = details.get("thinking_tokens")
    row.update(
        stop_reason=payload.get("stop_reason"),
        blocks=[
            b.get("type") + (f":{b.get('name')}" if b.get("type") == "tool_use" else "")
            for b in payload.get("content") or []
        ],
        input_tokens=usage.get("input_tokens"),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
        cache_read_input_tokens=usage.get("cache_read_input_tokens"),
        output_tokens=output_tokens,
        thinking_tokens=thinking_tokens,
        visible_tokens=(
            output_tokens - thinking_tokens
            if isinstance(output_tokens, int) and isinstance(thinking_tokens, int)
            else None
        ),
    )
    return row


def plan_requests(
    captured: dict[str, dict[str, Any]],
) -> list[tuple[str, str, dict[str, Any]]]:
    """Every approved call, built from the captured body for its model."""
    missing = sorted({m for m, _ in APPROVED_PLAN} - set(captured))
    if missing:
        raise ValueError(f"no captured request for {missing}")
    return [(m, v, build_variant(captured[m], m, v)) for m, v in APPROVED_PLAN]


def bound_violations(calls: list[tuple[str, str, dict[str, Any]]]) -> list[str]:
    """Every way a planned body leaves the approved bounds, naming its model and the bound."""
    found = []
    for model, variant, body in calls:
        where = f"{model} {variant}"
        max_tokens = body.get("max_tokens")
        if not isinstance(max_tokens, int) or max_tokens > MAX_TOKENS:
            found.append(
                f"{where}: max_tokens {max_tokens!r} is not at most {MAX_TOKENS}"
            )
        if "stream" in body:
            found.append(f"{where}: carries stream")
        for tool in body.get("tools") or []:
            if not isinstance(tool, dict) or "input_schema" not in tool:
                name = tool.get("name") if isinstance(tool, dict) else tool
                found.append(
                    f"{where}: tool {name!r} has no input_schema (a server tool)"
                )
        chars = len(json.dumps(body))
        if chars > MAX_BODY_CHARS:
            found.append(f"{where}: {chars} characters, over {MAX_BODY_CHARS}")
    return found


def completed_pairs(out: Path) -> set[tuple[str, str]]:
    """The (model, variant) pairs that already have a row in ``out``, success or failure.

    A line that names no pair raises ``ValueError``: its call may have been sent.
    """
    if not out.exists():
        return set()
    done = set()
    for n, line in enumerate(out.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            done.add((row["model"], row["variant"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(
                f"{out}:{n} is not a result row ({type(exc).__name__})"
            ) from None
    return done


def _redact(value: Any, key: str) -> Any:
    """``value`` with ``key`` replaced by ``<redacted>`` in every string it holds."""
    if isinstance(value, str):
        return value.replace(key, REDACTED)
    if isinstance(value, dict):
        return {k: _redact(v, key) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, key) for v in value]
    return value


def _append(path: Path, record: dict[str, Any], key: str) -> str:
    """Append ``record``, redacted of ``key``, to ``path`` as one JSON line; return the line.

    Every result row and raw response passes through here, so error text is
    never clipped before redaction (a clip could leave a prefix of the key).
    """
    line = json.dumps(_redact(record, key))
    with path.open("a") as f:
        f.write(line + "\n")
    return line


def _send(
    body: dict[str, Any], key: str, timeout: float
) -> tuple[int, dict[str, Any], float]:
    import httpx

    t0 = time.monotonic()
    r = httpx.post(
        API_URL,
        headers={
            "x-api-key": key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
        json=body,
        timeout=timeout,
    )
    try:
        payload = r.json()
    except ValueError:
        payload = {"error": {"message": r.text[:300]}}
    return r.status_code, payload, time.monotonic() - t0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--request", action="append", default=[], metavar="MODEL=PATH")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument(
        "--send",
        action="store_true",
        help="make the live calls; without it the plan is printed and nothing is sent",
    )
    args = p.parse_args(argv)

    planned_models = {m for m, _ in APPROVED_PLAN}
    captured: dict[str, dict[str, Any]] = {}
    for spec in args.request:
        model, _, path = spec.partition("=")
        if model not in planned_models:
            print(
                f"refused: --request {model!r} is not in APPROVED_PLAN", file=sys.stderr
            )
            return 2
        if model in captured:
            print(f"refused: --request {model!r} is given twice", file=sys.stderr)
            return 2
        doc = json.loads(Path(path).read_text())
        captured[model] = doc.get("body", doc)
    calls = plan_requests(captured)
    violations = bound_violations(calls)
    if violations:
        for violation in violations:
            print(f"refused: {violation}", file=sys.stderr)
        return 2

    if not args.send:
        for model, variant, body in calls:
            print(
                f"{model:18} {variant:10} tool_choice={body.get('tool_choice')} "
                f"thinking={body.get('thinking')} output_config={body.get('output_config')} "
                f"max_tokens={body.get('max_tokens')} chars={len(json.dumps(body))}"
            )
        return 0

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2
    if any(c.isspace() or unicodedata.category(c) == "Cc" for c in key):
        print(
            "refused: ANTHROPIC_API_KEY contains whitespace or a control character",
            file=sys.stderr,
        )
        return 2
    try:
        done = completed_pairs(args.out)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    raw_out = args.out.with_name(args.out.name + ".raw.jsonl")
    for model, variant, body in calls:  # sequential, each at most once, no retry
        if (model, variant) in done:
            skipped = {"model": model, "variant": variant, "skipped": str(args.out)}
            print(json.dumps(skipped), flush=True)
            continue
        try:
            status, payload, elapsed = _send(body, key, args.timeout)
        except Exception as exc:  # a transport failure is a result, not a retry
            row = {"http": None, "error": f"{type(exc).__name__}: {exc}"}
        else:
            raw = {"model": model, "variant": variant, "http": status}
            _append(raw_out, {**raw, "response": payload}, key)
            try:
                row = summarize_response(status, payload, elapsed)
            except Exception as exc:  # the raw line above keeps the paid response
                row = {
                    "http": status,
                    "elapsed_s": round(elapsed, 1),
                    "summary_error": f"{type(exc).__name__}: {exc}",
                }
        line = _append(args.out, {"model": model, "variant": variant, **row}, key)
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
