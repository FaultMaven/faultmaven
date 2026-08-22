#!/usr/bin/env python3
"""The API contract's surface cannot change without its version moving.

``docs/reference/api/openapi.json`` is the contract between this API and its
clients. `api-contract-drift` already holds it byte-equal to what the code
serves, so the document cannot lie about the server — but that check is
satisfied by regenerating, which is exactly what a change to a route does
automatically. Nothing yet distinguished "the contract was deliberately
amended" from "the artifact moved because someone edited a handler".

This is that distinction. If the structural surface differs from the base
branch while `info.version` stays put, the pull request fails and says what
changed. Bumping is the act of publishing (see
``faultmaven/api/contract_version.py``); the clients then adopt by moving the
ref they pin.

**Structural, not textual.** Descriptions, summaries, titles and examples are
stripped before comparing: no client breaks on a reworded docstring, and
demanding a bump for prose would teach people to bump without reading the
diff. What remains — paths, operations, parameters, request bodies, response
codes, schemas — is what a client is written against.

**It does not judge MINOR versus MAJOR.** Whether a change is one a client
survives is the question the clients are being asked, and a script that
answered it would be making the agreement on their behalf.

Usage:

    python scripts/check_contract_version.py                 # vs origin/main
    python scripts/check_contract_version.py --base HEAD~1   # vs the last commit

Exit codes: 0 the contract is consistent, 1 a surface change needs a version
bump, 2 the base contract could not be read (never a silent pass — a gate that
skips when it cannot see is worse than no gate).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPEC_RELPATH = "docs/reference/api/openapi.json"
SPEC_PATH = PROJECT_ROOT / SPEC_RELPATH

#: Keys carrying prose or illustration rather than structure. A client is not
#: written against any of them.
_PROSE_KEYS = frozenset(
    {"description", "summary", "title", "example", "examples", "externalDocs"}
)

#: Keys whose *value* is a mapping of author-chosen names, not of OpenAPI
#: keywords. Prose filtering must not reach inside these: a schema property
#: genuinely called `title` or `description` — which cases, runbooks and
#: knowledge documents all have — would otherwise be invisible to the
#: comparison, and dropping one from a response would pass this gate silently.
_NAME_MAP_KEYS = frozenset(
    {
        "properties",
        "patternProperties",
        "schemas",
        "definitions",
        "$defs",
        "headers",
        "content",
        "paths",
        "responses",
        "encoding",
        "links",
        "callbacks",
    }
)


def _strip_prose(node: Any, *, in_name_map: bool = False) -> Any:
    """The same document with prose keywords removed, recursively.

    ``in_name_map`` marks a dict whose keys were chosen by whoever wrote the
    API — field names, schema names, status codes, media types. Those keys are
    kept whatever they are called; their values are ordinary objects and are
    filtered normally.

    That last clause is the whole subtlety, and it has to key on **position**
    rather than on the child's name. Deciding from the name alone gets it wrong
    in both directions: a property named `title` looks like prose (dropping it
    from a response would be invisible to the gate), and a property named
    `content` looks like a name map (its description would survive stripping,
    so rewording it would demand a version bump for prose). Six schemas here
    really do have a `content` property — CaseReport, ReportResponse,
    ReportUpdateRequest, DraftUpdateRequest, Message, KnowledgeBaseDocument.
    """
    if isinstance(node, dict):
        if in_name_map:
            # Author-chosen keys. Keep every one of them, and step back into
            # ordinary filtering for the objects they point at.
            return {
                key: _strip_prose(value, in_name_map=False)
                for key, value in node.items()
            }
        return {
            key: _strip_prose(value, in_name_map=key in _NAME_MAP_KEYS)
            for key, value in node.items()
            if key not in _PROSE_KEYS
        }
    if isinstance(node, list):
        return [_strip_prose(item) for item in node]
    return node


def _surface(spec: Dict[str, Any]) -> Dict[str, Any]:
    """The part of a contract a client can break on."""
    return {
        "paths": _strip_prose(spec.get("paths", {})),
        "components": _strip_prose(spec.get("components", {})),
    }


def _version(spec: Dict[str, Any]) -> str:
    return str(spec.get("info", {}).get("version", ""))


#: A published contract version is exactly MAJOR.MINOR.PATCH. Parsed rather
#: than compared as text so the gate can require an INCREASE: accepting any
#: inequality would pass a PR that changed the surface while lowering the
#: version (1.0.0 -> 0.9.0), and would read a typo (`1.O.0`, letter O) as a
#: deliberate publication.
_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _version_tuple(version: str) -> Optional[Tuple[int, int, int]]:
    match = _VERSION_PATTERN.match(version)
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def read_base_spec(ref: str) -> Dict[str, Any]:
    """The committed contract at ``ref``.

    Raises:
        RuntimeError: the ref or the file is not readable — in CI that means
            the base branch was not fetched, which must fail loudly rather
            than let the check pass having compared nothing.
    """
    try:
        blob = subprocess.run(
            ["git", "show", f"{ref}:{SPEC_RELPATH}"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise RuntimeError(
            f"could not read {SPEC_RELPATH} at '{ref}'. In CI, fetch the base "
            f"branch first (git fetch --depth=1 origin main)."
        ) from exc

    try:
        return json.loads(blob)
    except ValueError as exc:
        raise RuntimeError(f"{SPEC_RELPATH} at '{ref}' is not valid JSON") from exc


def describe_surface_change(base: Dict[str, Any], head: Dict[str, Any]) -> List[str]:
    """Human-readable lines naming what moved, for the bump decision.

    The reader has to choose MINOR or MAJOR and tell the client owners what
    they are accepting; a bare "the surface changed" leaves them diffing a
    generated document by hand.
    """
    lines: List[str] = []
    base_paths, head_paths = base["paths"], head["paths"]

    for path in sorted(set(head_paths) - set(base_paths)):
        lines.append(f"  + path      {path}")
    for path in sorted(set(base_paths) - set(head_paths)):
        lines.append(f"  - path      {path}")

    for path in sorted(set(base_paths) & set(head_paths)):
        if base_paths[path] == head_paths[path]:
            continue
        base_ops, head_ops = base_paths[path], head_paths[path]
        for method in sorted(set(head_ops) - set(base_ops)):
            lines.append(f"  + operation {method.upper()} {path}")
        for method in sorted(set(base_ops) - set(head_ops)):
            lines.append(f"  - operation {method.upper()} {path}")
        for method in sorted(set(base_ops) & set(head_ops)):
            if base_ops[method] == head_ops[method]:
                continue
            lines.append(f"  ~ operation {method.upper()} {path}")
            lines.extend(_describe_operation(base_ops[method], head_ops[method]))

    # Every section of `components`, not only `schemas`: `_surface` compares
    # the whole object, so a change confined to `securitySchemes` (or
    # `responses`, `parameters`, `headers`) would otherwise fail the gate with
    # an EMPTY delta — "the surface changed" and nothing else, which leaves the
    # reader diffing a generated document by hand.
    base_components, head_components = base["components"], head["components"]
    for section in sorted(set(base_components) | set(head_components)):
        base_section = base_components.get(section, {})
        head_section = head_components.get(section, {})
        if base_section == head_section:
            continue
        if not isinstance(base_section, dict) or not isinstance(head_section, dict):
            lines.append(f"  ~ components.{section}")
            continue
        # `schemas` is the common case and reads better unqualified.
        label = "schema   " if section == "schemas" else f"{section}"
        for name in sorted(set(head_section) - set(base_section)):
            lines.append(f"  + {label} {name}")
        for name in sorted(set(base_section) - set(head_section)):
            lines.append(f"  - {label} {name}")
        for name in sorted(set(base_section) & set(head_section)):
            if base_section[name] != head_section[name]:
                lines.append(f"  ~ {label} {name}")

    return lines


def _describe_operation(base_op: Any, head_op: Any) -> List[str]:
    """The two facets of an operation a client most often breaks on."""
    lines: List[str] = []
    if not isinstance(base_op, dict) or not isinstance(head_op, dict):
        return lines

    base_codes = set(base_op.get("responses", {}))
    head_codes = set(head_op.get("responses", {}))
    if base_codes != head_codes:
        lines.append(f"      responses {sorted(base_codes)} -> {sorted(head_codes)}")

    base_types = set(base_op.get("requestBody", {}).get("content", {}))
    head_types = set(head_op.get("requestBody", {}).get("content", {}))
    if base_types != head_types:
        lines.append(
            f"      request content {sorted(base_types)} -> {sorted(head_types)}"
        )

    return lines


def run_check(base_ref: str) -> Tuple[int, List[str]]:
    """Returns ``(exit_code, report_lines)``."""
    try:
        base_spec = read_base_spec(base_ref)
    except RuntimeError as exc:
        return 2, [f"❌ {exc}"]

    if not SPEC_PATH.exists():
        return 2, [f"❌ {SPEC_RELPATH} is missing; it is generated and committed."]

    try:
        head_spec = json.loads(SPEC_PATH.read_text())
    except (OSError, ValueError) as exc:
        # Guarded like the base read: unguarded, this exits 1 with a traceback,
        # and 1 is the code that means "a surface change needs a version bump".
        return 2, [f"❌ {SPEC_RELPATH} could not be read: {exc}"]

    base_surface, head_surface = _surface(base_spec), _surface(head_spec)
    base_version, head_version = _version(base_spec), _version(head_spec)

    if base_surface == head_surface:
        if base_version != head_version:
            return 0, [
                f"✅ Contract version {base_version} -> {head_version} with no "
                "structural change (a re-publication)."
            ]
        return 0, [f"✅ API contract unchanged (version {head_version})."]

    changes = describe_surface_change(base_surface, head_surface)

    if base_version != head_version:
        base_parts = _version_tuple(base_version)
        head_parts = _version_tuple(head_version)
        if head_parts is None:
            return 1, [
                f"❌ API_CONTRACT_VERSION is {head_version!r}, which is not "
                "MAJOR.MINOR.PATCH. A version that cannot be ordered cannot "
                "tell a client whether it is behind.",
            ]
        if base_parts is not None and head_parts < base_parts:
            return 1, [
                f"❌ The contract version went BACKWARDS, {base_version} -> "
                f"{head_version}, alongside a surface change:",
                *changes,
                "",
                "   Publishing moves the version forward. A client comparing "
                "what it pinned against what is published would read this as "
                "already adopted.",
            ]
        return 0, [
            f"✅ API contract {base_version} -> {head_version}:",
            *changes,
            "",
            "   Clients adopt this by moving the ref they pin — it does not "
            "reach them on merge.",
        ]

    return 1, [
        f"❌ The API contract's surface changed while info.version stayed at "
        f"{head_version}:",
        *changes,
        "",
        "   The contract is what the clients are written against, so a surface",
        "   change is an amendment they have to accept. Publish it by bumping",
        "   API_CONTRACT_VERSION in faultmaven/api/contract_version.py and",
        "   regenerating:",
        "",
        "       python scripts/generate_api_docs.py",
        "",
        "   MINOR if every existing client survives it, MAJOR if one can break.",
        "   Then open the pin bump in each client that adopts it:",
        "   faultmaven-copilot, faultmaven-dashboard, faultmaven-slack-agent.",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base",
        default="origin/main",
        help="git ref holding the contract to compare against (default: origin/main)",
    )
    args = parser.parse_args()

    exit_code, report = run_check(args.base)
    for line in report:
        print(line)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
