#!/usr/bin/env python3
"""Generate the checked-in API reference from the live FastAPI app.

Two artifacts, both derived from ``app.openapi()`` and nothing else:

- ``docs/reference/api/openapi.json`` — the specification, canonical JSON
- ``docs/reference/api/README.md`` — a human-readable rendering of it

Run with ``--check`` to verify the committed artifacts match the app instead of
rewriting them. That is what CI runs: it is the gate that stops the artifacts
drifting away from the routes they describe (issue #880).

Both outputs are byte-reproducible. Nothing here injects a timestamp, a
hand-written description, or an example — an artifact that is partly generated
and partly hand-authored cannot be checked against its source, and that is how
the previous version of this script let the spec claim the API needed no
authentication for months after it did.

Usage:
    python scripts/generate_api_docs.py            # rewrite the artifacts
    python scripts/generate_api_docs.py --check    # fail if they are stale
"""

import argparse
import difflib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# Environment the document is generated under. Several routers are mounted
# conditionally, so the spec is a function of configuration as well as of code:
# debug endpoints (``ENVIRONMENT``/``ENABLE_DEBUG_ENDPOINTS``), the OAuth router
# (``OAUTH_ENABLED``), the SSO router (``AUTH_MODE`` plus the WorkOS trio) and
# ``/metrics`` (``METRICS_EXPORTER``). Unpinned, the same commit yields a
# different document on a laptop than in CI and the drift gate reads that as a
# contract change.
#
# The reference documents the **maximal deployed surface**: every route the
# product can serve, so one generated client covers every deployment. Debug
# endpoints are the exception — they are development-only and are never part of
# a deployed API. Excluding OAuth and SSO would leave the document advertising
# `/auth/oauth/authorize` and `/auth/sso/login` from `GET /auth/config` while
# describing neither.
PINNED_ENVIRONMENT = {
    # Building a document must not reach a database, Redis or an LLM provider.
    "SKIP_SERVICE_CHECKS": "true",
    # Not development: excludes the debug router.
    "ENVIRONMENT": "production",
    # Only present to satisfy the startup validator that rejects wildcard CORS
    # in production. CORS is middleware — it appears nowhere in the document.
    "CORS_ALLOW_ORIGINS": '["https://app.faultmaven.com"]',
    # Mount the OAuth and SSO routers. The WorkOS values are placeholders that
    # satisfy `sso_configured`; no credential is contacted while building a
    # schema, and none of these reach the document.
    "AUTH_MODE": "oauth",
    "OAUTH_ENABLED": "true",
    "WORKOS_API_KEY": "placeholder-not-a-credential",
    "WORKOS_CLIENT_ID": "placeholder-not-a-credential",
    "WORKOS_REDIRECT_URI": "https://app.faultmaven.com/auth/sso/callback",
    # Mount /metrics.
    "METRICS_EXPORTER": "prometheus_http",
}

# Variables the interpreter and its imports need, kept when the environment is
# emptied. Everything else is discarded rather than enumerated, so a setting
# nobody thought to pin cannot reach the document — which is how
# ``METRICS_EXPORTER`` was missed the first time this was written.
_SYSTEM_ENVIRONMENT_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "PWD",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONHASHSEED",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SYSTEMROOT",
        "COMSPEC",
    }
)


def _pin_generation_environment() -> None:
    """Make the spec a function of the code, not of whoever runs this.

    Empties the environment down to ``_SYSTEM_ENVIRONMENT_KEYS`` and then
    applies ``PINNED_ENVIRONMENT``. Discarding by default rather than
    overriding a known list is deliberate: the list is what goes stale when a
    new conditional router arrives.

    ``.env`` loading is neutralised for the same reason — a local file that
    pydantic-settings would otherwise read is exactly the ambient state that
    makes an artifact irreproducible.
    """
    try:
        import dotenv

        dotenv.load_dotenv = lambda *args, **kwargs: None
        dotenv.dotenv_values = lambda *args, **kwargs: {}
    except ImportError:
        pass

    preserved = {
        key: value
        for key, value in os.environ.items()
        if key in _SYSTEM_ENVIRONMENT_KEYS
    }
    os.environ.clear()
    os.environ.update(preserved)
    os.environ.update(PINNED_ENVIRONMENT)


_pin_generation_environment()

DOCS_DIR = PROJECT_ROOT / "docs" / "reference" / "api"
SPEC_PATH = DOCS_DIR / "openapi.json"
MARKDOWN_PATH = DOCS_DIR / "README.md"

METHOD_ORDER = ["get", "post", "put", "patch", "delete", "head", "options", "trace"]


def build_spec() -> Dict[str, Any]:
    """Return the app's OpenAPI document, unmodified."""
    from faultmaven.main import app

    return app.openapi()


def render_spec(spec: Dict[str, Any]) -> str:
    """Serialise the spec canonically, so equal specs give equal bytes."""
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def _operations(path_item: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """The HTTP operations of a path item, in a stable order.

    OpenAPI path items also carry non-operation keys (``parameters``,
    ``summary``, ``x-`` extensions); only verbs are operations.
    """
    ops = [
        (method, details)
        for method, details in path_item.items()
        if method in METHOD_ORDER and isinstance(details, dict)
    ]
    return sorted(ops, key=lambda item: METHOD_ORDER.index(item[0]))


def _schema_label(schema: Dict[str, Any]) -> str:
    """Name a schema as a reader can follow it back to the Data Models section.

    A ``$ref`` becomes a link to that model's heading; anything inline is named
    by its type. Without this, every request body and response in the reference
    reads as an untyped blank.
    """
    if not schema:
        return "no schema"

    ref = schema.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        return f"[`{name}`](#{name.lower()})"

    if schema.get("type") == "array":
        return f"array of {_schema_label(schema.get('items', {}))}"

    for combinator in ("anyOf", "oneOf", "allOf"):
        if schema.get(combinator):
            parts = [_schema_label(option) for option in schema[combinator]]
            return " | ".join(dict.fromkeys(parts))

    return f"`{schema.get('type', 'object')}`"


def _describe_auth(operation: Dict[str, Any]) -> str:
    """Say what authentication an operation requires, per the spec.

    ``security`` absent or empty means the route is reachable without
    credentials. This is the line the artifacts previously got wrong.
    """
    requirements = operation.get("security")
    if not requirements:
        return "None — this operation is reachable unauthenticated."

    alternatives = []
    for requirement in requirements:
        if not requirement:
            alternatives.append("none (optional)")
            continue
        for scheme, scopes in sorted(requirement.items()):
            alternatives.append(
                f"`{scheme}`" + (f" (scopes: {', '.join(scopes)})" if scopes else "")
            )
    return " or ".join(alternatives)


def render_markdown(spec: Dict[str, Any]) -> str:
    """Render the spec as markdown. Deterministic — no clock, no hand-edits."""
    info = spec.get("info", {})
    schemes = spec.get("components", {}).get("securitySchemes", {})

    lines: List[str] = [
        "# FaultMaven API Reference",
        "",
        "<!-- Generated by scripts/generate_api_docs.py from the live FastAPI",
        "     app. Do not edit by hand — CI regenerates this and fails if it",
        "     differs. -->",
        "",
        f"**Version:** {info.get('version', 'unknown')}",
        "",
    ]

    if info.get("description"):
        lines += [info["description"].strip(), ""]

    lines += ["## Authentication", ""]
    if schemes:
        for name, definition in sorted(schemes.items()):
            detail = definition.get("scheme") or definition.get("type", "")
            bearer = definition.get("bearerFormat")
            suffix = f", {bearer}" if bearer else ""
            lines.append(f"- `{name}` — {definition.get('type', '')} {detail}{suffix}")
        lines += [
            "",
            "Each operation below states the scheme it requires. Operations "
            "that state `None` are reachable without credentials.",
            "",
        ]
    else:
        lines += ["No security schemes are declared by the API.", ""]

    lines += ["## Endpoints", ""]

    for path, path_item in sorted(spec.get("paths", {}).items()):
        lines += [f"### `{path}`", ""]

        for method, details in _operations(path_item):
            lines += [f"#### {method.upper()}", ""]

            if details.get("summary"):
                lines += [f"**{details['summary']}**", ""]
            if details.get("description"):
                lines += [details["description"].strip(), ""]
            if details.get("tags"):
                tags = ", ".join(f"`{tag}`" for tag in details["tags"])
                lines += [f"**Tags:** {tags}", ""]

            lines += [f"**Auth:** {_describe_auth(details)}", ""]

            if details.get("parameters"):
                lines += ["**Parameters:**", ""]
                for parameter in details["parameters"]:
                    required = "required" if parameter.get("required") else "optional"
                    description = parameter.get("description", "")
                    suffix = f" — {description}" if description else ""
                    lines.append(
                        f"- `{parameter['name']}` "
                        f"({parameter.get('in', 'query')}, {required}){suffix}"
                    )
                lines.append("")

            if details.get("requestBody"):
                body = details["requestBody"]
                required = "required" if body.get("required") else "optional"
                lines += [f"**Request body** ({required}):", ""]
                for content_type, content in sorted(body.get("content", {}).items()):
                    schema = _schema_label(content.get("schema", {}))
                    lines.append(f"- `{content_type}` — {schema}")
                lines.append("")

            if details.get("responses"):
                lines += ["**Responses:**", ""]
                for status, response in sorted(details["responses"].items()):
                    entry = f"- `{status}` — {response.get('description', '')}".rstrip()
                    schemas = sorted(
                        {
                            _schema_label(content.get("schema", {}))
                            for content in response.get("content", {}).values()
                            if content.get("schema")
                        }
                    )
                    if schemas:
                        entry += f" ({', '.join(schemas)})"
                    lines.append(entry)
                lines.append("")

            lines += ["---", ""]

    schemas = spec.get("components", {}).get("schemas", {})
    if schemas:
        lines += ["## Data Models", ""]
        for name, definition in sorted(schemas.items()):
            lines += [f"### {name}", ""]
            if definition.get("description"):
                lines += [definition["description"].strip(), ""]

            if definition.get("enum"):
                values = ", ".join(f"`{value}`" for value in definition["enum"])
                lines += [f"**Values:** {values}", ""]

            if definition.get("properties"):
                required_names = set(definition.get("required", []))
                lines += ["**Properties:**", ""]
                for prop, prop_def in sorted(definition["properties"].items()):
                    required = "required" if prop in required_names else "optional"
                    prop_type = prop_def.get("type", "object")
                    description = prop_def.get("description", "")
                    suffix = f" — {description}" if description else ""
                    lines.append(f"- `{prop}` ({prop_type}, {required}){suffix}")
                lines.append("")

            lines += ["---", ""]

    return "\n".join(lines) + "\n"


def _report_stale(path: Path, expected: str) -> bool:
    """Print a diff for one artifact. Returns True when it is stale."""
    actual = path.read_text() if path.exists() else ""
    if actual == expected:
        return False

    print(f"\n❌ {path.relative_to(PROJECT_ROOT)} does not match the running app.")
    diff = difflib.unified_diff(
        actual.splitlines(keepends=True),
        expected.splitlines(keepends=True),
        fromfile=f"{path.name} (committed)",
        tofile=f"{path.name} (app)",
        n=1,
    )
    shown = 0
    for line in diff:
        if shown >= 60:
            print("   … diff truncated; regenerate to see it in full.")
            break
        print("   " + line.rstrip("\n"))
        shown += 1
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the committed artifacts differ from the app (used by CI)",
    )
    args = parser.parse_args()

    spec = build_spec()
    outputs = {SPEC_PATH: render_spec(spec), MARKDOWN_PATH: render_markdown(spec)}

    if args.check:
        stale = [
            path for path, content in outputs.items() if _report_stale(path, content)
        ]
        if stale:
            print(
                "\nThe API reference no longer describes the routes this app "
                "serves. Regenerate and commit it in the same change:\n"
                "\n    python scripts/generate_api_docs.py"
                f"\n    git add {' '.join(str(p.relative_to(PROJECT_ROOT)) for p in outputs)}\n"
                "\nIf the diff is in schema *shape* rather than in routes — "
                "keys like `ctx`/`input` on ValidationError, `const` vs a "
                "single-value `enum`, `contentMediaType` vs `format: binary` — "
                "then the artifact was generated against different library "
                "versions than CI uses. The document is a function of the code "
                "AND the pinned toolchain, because FastAPI and Pydantic decide "
                "how schemas are emitted. Regenerate with the lockfile "
                "installed:\n"
                "\n    pip install -r requirements/dev.txt\n"
            )
            return 1
        print("✅ API reference matches the running app.")
        return 0

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    for path, content in outputs.items():
        path.write_text(content)
        print(f"💾 {path.relative_to(PROJECT_ROOT)}")

    operations = sum(len(_operations(item)) for item in spec.get("paths", {}).values())
    secured = sum(
        1
        for item in spec.get("paths", {}).values()
        for _, details in _operations(item)
        if details.get("security")
    )
    print(
        f"\n{len(spec.get('paths', {}))} paths, {operations} operations "
        f"({secured} requiring authentication), "
        f"{len(spec.get('components', {}).get('schemas', {}))} schemas."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
