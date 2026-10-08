"""The runbook spec's closed vocabularies, read from its §Taxonomy Schema table.

Tests that check what a reader renders compare it with THIS, not with
``faultmaven.modules.knowledge.taxonomy``: a test that compares the code's
rendering with the code's own vocabulary compares the code with itself, and a
renderer that dropped a value would pass it (#1886 review).
"""

from __future__ import annotations

import re
from pathlib import Path

SPEC = (
    Path(__file__).resolve().parents[1]
    / "docs/architecture/knowledge-and-ai/runbook-content-architecture.md"
)


def spec_vocabularies() -> dict[str, list[str]]:
    """Field -> the backticked values in its Purpose cell, in the table's order."""
    text = SPEC.read_text(encoding="utf-8")
    section = text.split("### Taxonomy Schema", 1)[1].split("\n### ", 1)[0]
    table: dict[str, list[str]] = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4 or not cells[0].startswith("`"):
            continue
        table[cells[0].strip("`")] = re.findall(r"`([^`]+)`", cells[3])
    return table
