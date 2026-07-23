"""Corpus hygiene: every shipped runbook's ``symptom_class`` is a well-formed
snake_case vocabulary token.

Phase 3 wired ``symptom_class`` into reranking/retrieval metadata, so an
off-vocabulary value (e.g. a hyphenated ``throughput-degradation`` instead of the
canonical ``throughput_degradation``) silently mis-tags a runbook and never
matches the curated term. The controlled ``symptom_class`` vocabulary itself is
owned by ``faultmaven-kb-toolkit`` (``config.py``) and enforced across repos by
the vocab-parity gate — this test deliberately does NOT re-encode that list
(which would drift). It guards only the *shape* of the values the app actually
ships: lowercase snake_case tokens, which is exactly what distinguishes the
canonical vocabulary from the hyphen/space/uppercase typos a shape guard can
catch without knowing the vocabulary.

The value is parsed with the app's real ``extract_frontmatter_metadata`` — the
same function the ingestion paths use to read ``symptom_class`` — so this pins
the tokens the runtime metadata actually carries.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from faultmaven.utils.frontmatter import extract_frontmatter_metadata

pytestmark = pytest.mark.unit

# tests/unit/modules/knowledge/<file> -> parents[4] == repo root
RUNBOOKS_DIR = (
    Path(__file__).resolve().parents[4] / "resources" / "knowledge" / "runbooks"
)

# A well-formed vocabulary token: lowercase, starts with a letter, then
# letters/digits/underscores. Rejects hyphens, spaces, and uppercase — the
# author typos a shape guard can catch without the vocabulary list itself.
_SNAKE_TOKEN = re.compile(r"[a-z][a-z0-9_]*")


def _shipped_runbooks() -> list[Path]:
    if not RUNBOOKS_DIR.exists():
        return []
    return [
        md
        for md in sorted(RUNBOOKS_DIR.rglob("*.md"))
        if md.read_text(encoding="utf-8").lstrip().startswith("---")
    ]


def test_all_symptom_class_values_are_wellformed_tokens():
    """No shipped runbook carries a malformed ``symptom_class`` value."""
    runbooks = _shipped_runbooks()
    # Guard against a broken path silently passing the whole check.
    assert runbooks, f"no shipped runbooks found under {RUNBOOKS_DIR}"

    checked = 0
    violations: list[str] = []
    for md in runbooks:
        meta = extract_frontmatter_metadata(md.read_text(encoding="utf-8"))
        raw = meta.get("symptom_class")
        if raw is None:
            continue
        checked += 1
        # extract_frontmatter_metadata joins the list to a comma-separated string.
        for token in raw.split(","):
            if not _SNAKE_TOKEN.fullmatch(token):
                violations.append(f"{md.relative_to(RUNBOOKS_DIR)}: {token!r}")

    assert checked, "no runbook exposed a symptom_class to check"
    assert not violations, (
        "malformed symptom_class values (must be lowercase snake_case, e.g. "
        "'throughput_degradation' not 'throughput-degradation'):\n  "
        + "\n  ".join(violations)
    )


def test_kafka_consumer_lag_symptom_class_regression():
    """Regression: the kafka-consumer-lag runbook once shipped a hyphenated
    ``throughput-degradation`` (off-vocabulary). It must carry the canonical
    underscore token."""
    md = RUNBOOKS_DIR / "messaging" / "kafka-consumer-lag.md"
    assert md.exists(), f"expected runbook missing: {md}"
    meta = extract_frontmatter_metadata(md.read_text(encoding="utf-8"))
    tokens = meta.get("symptom_class", "").split(",")
    assert "throughput_degradation" in tokens
    assert "throughput-degradation" not in tokens
