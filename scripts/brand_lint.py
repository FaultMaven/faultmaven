#!/usr/bin/env python3
"""Brand-messaging lint: fail on retired terminology in brand-facing files.

Canonical source: ``.claude/skills/brand-messaging.md`` (§3 terminology, §7
enforcement). This is the automation §7 calls for — a downstream check; the skill
remains the source of truth.

Two pattern classes, per the skill's "Authority by rule type":

* ``UNIVERSAL`` — terminology. Must not appear on ANY brand-facing surface,
  including marketing copy. Runs on every file in ``UNIVERSAL_FILES``.
* ``CORE_ONLY`` — positioning / audience / tone. Enforced on core product
  surfaces (README, product descriptions), NOT marketing copy or the dev guide.

This repo (faultmaven CE) has no marketing surfaces. Downstream repos
(faultmaven-website, -copilot, -dashboard) carry their own copy of this check
scoped to their own surfaces.

Put ``brand-lint: allow`` anywhere on a line to whitelist a deliberate, justified
use. The brand skill and the ``/sync-brand`` command are intentionally NOT scanned
— they contain the pattern list itself.

Usage: ``python scripts/brand_lint.py`` (scans the fixed brand-file set).
Exits non-zero if any violation is found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOW_MARKER = "brand-lint: allow"

# Brand-facing files that describe FaultMaven as a product. NOT technical/
# architecture docs, NOT the brand skill/command (they hold the pattern list).
UNIVERSAL_FILES = [
    "README.md",
    "CLAUDE.md",
    "pyproject.toml",
    "docs/README.md",
]

# Core product surfaces where positioning/tone rules also apply. Excludes
# CLAUDE.md (a developer/AI guide, not product copy) to avoid tone false matches.
CORE_FILES = [
    "README.md",
    "pyproject.toml",
    "docs/README.md",
]

# (regex, guidance). Patterns mirror brand-messaging.md §3/§7.
# NOTE: 'AIOps platform' / 'observability platform' / 'playbook' are deliberately
# NOT grepped — FaultMaven references those categories by contrast (intended
# positioning), so a substring match false-positives. They stay §3 review rules.
UNIVERSAL = [
    (r"\btroubleshooting assistant\b", "use 'troubleshooting copilot', not 'troubleshooting assistant'"),
    (r"\bmicroservices?\s+backend\b", "FaultMaven is a modular monolith, not microservices"),
    (r"\bLocal Deployment\b", "use 'Standalone' (ADR-004); 'local' is reserved for AUTH_MODE/CHAT_PROVIDER"),
    (r"\bdeploy locally\b", "use 'self-host' / 'Standalone' (ADR-004)"),
    (r"\bEnterprise SaaS\b", "use 'FaultMaven Cloud'; there is no Enterprise tier"),
    (r"\bfaultmaven-deploy\b", "obsolete repo — do not reference"),
    (r"\bfm-[a-z]+-service\b", "obsolete microservice repo — do not reference"),
]

CORE_ONLY = [
    (r"\bfor SRE teams\b", "don't narrow the audience to one role (brand §4)"),
    (r"\bdesigned for DevOps\b", "don't narrow the audience to one role (brand §4)"),
    (r"\bleverages?\b", "use a precise verb (uses/reads/queries…), not 'leverage' (brand §5)"),
    (r"\butiliz(?:e|es|ed|ing|ation)\b", "use 'use', not 'utilize' (brand §5)"),
]

_COMPILED = {
    "UNIVERSAL": [(re.compile(p, re.IGNORECASE), m) for p, m in UNIVERSAL],
    "CORE_ONLY": [(re.compile(p, re.IGNORECASE), m) for p, m in CORE_ONLY],
}


def _scan(rel_path: str, rules: list, hits: list) -> None:
    path = ROOT / rel_path
    if not path.exists():
        return
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for pattern, message in rules:
            if pattern.search(line):
                hits.append(f"{rel_path}:{lineno}: '{pattern.pattern}' — {message}")


def main() -> int:
    hits: list = []
    for rel in UNIVERSAL_FILES:
        _scan(rel, _COMPILED["UNIVERSAL"], hits)
    for rel in CORE_FILES:
        _scan(rel, _COMPILED["CORE_ONLY"], hits)

    if hits:
        print("Brand-messaging lint failed (canonical: .claude/skills/brand-messaging.md):\n")
        for h in hits:
            print(f"  {h}")
        print(
            "\nFix the wording, or append 'brand-lint: allow' to the line for a "
            "deliberate, justified use.\nWhen retiring a NEW term, add it here AND "
            "to brand-messaging.md §7 in the same change."
        )
        return 1

    print("Brand-messaging lint passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
