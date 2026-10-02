"""Measure which control bounds thinking on Claude Opus 5, Opus 5.5 and Fable 5.1 (#1800).

Each call replays a REAL FaultMaven INVESTIGATING request, captured offline
from the API (no network) for that model, with one thinking control applied:

* ``omitted``    the body exactly as FaultMaven sends it for ``ANTHROPIC_THINKING_MODE=off``;
* ``effort_low`` the same body plus ``output_config: {"effort": "low"}``;
* ``disabled``   the same body plus ``thinking: {"type": "disabled"}``.

The live calls cost money, so the plan is fixed: ``APPROVED_PLAN`` is the set the
owner approved on #1800 (2026-09-30), every call is sent at most once, nothing
is retried, and any pair outside the plan is refused. ``--dry-run`` prints what
would be sent and sends nothing.

    python scripts/anthropic_thinking_controls.py \
        --request claude-opus-5=req-opus5.json --request claude-opus-5-5=req-opus55.json \
        --request claude-fable-5-1=req-fable51.json --out results.jsonl [--dry-run]

The API key is read from ``ANTHROPIC_API_KEY``; it is never printed.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
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
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    captured: dict[str, dict[str, Any]] = {}
    for spec in args.request:
        model, _, path = spec.partition("=")
        doc = json.loads(Path(path).read_text())
        captured[model] = doc.get("body", doc)
    calls = plan_requests(captured)

    if args.dry_run:
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
    with args.out.open("a") as f:
        for model, variant, body in calls:  # sequential, each once, no retry
            try:
                status, payload, elapsed = _send(body, key, args.timeout)
                row = summarize_response(status, payload, elapsed)
            except Exception as exc:  # a transport failure is a result, not a retry
                row = {"http": None, "error": f"{type(exc).__name__}: {exc}"[:300]}
            row = {"model": model, "variant": variant, **row}
            f.write(json.dumps(row) + "\n")
            f.flush()
            print(json.dumps(row), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
