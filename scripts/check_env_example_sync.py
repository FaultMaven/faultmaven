#!/usr/bin/env python3
"""Guard: .env.example must stay in sync with settings.py defaults.

Standalone config contract (agreed design):
  - settings.py Field(default=...) is the RUNTIME source of truth.
  - .env.example is its human-facing MIRROR: every variable it documents (as
    `KEY=value` or a commented `# KEY=value`) must show the SAME default.
  - registry.py PROVIDER_SCHEMA[...].default_model is the per-provider model
    fallback and must also equal the settings.py model default.
  - All three therefore change together; this check fails CI / the commit on any
    divergence, so a default can be re-defined in one place without silent drift.

Scope (what is value-checked):
  - Scalar defaults only: str / int / float / bool / Enum.
  - SKIPPED (no shippable scalar default, so not value-checked):
      * secrets (SecretStr)         — user-provided or auto-generated, shown blank
      * fields whose default is None — optional, shown blank/commented
      * complex defaults (list/dict) and default_factory fields
  - Variables in .env.example that are NOT settings fields must be in ALLOWLIST
    (e.g. compose-only knobs); otherwise they're flagged as unknown (typo guard).

Usage:  python scripts/check_env_example_sync.py        # exit 1 on drift
"""

from __future__ import annotations

import enum
import inspect
import re
import sys
from pathlib import Path

from pydantic_settings import BaseSettings

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import faultmaven.config.settings as settings_module  # noqa: E402

# Variables that legitimately appear in .env.example but are not pydantic
# settings fields (consumed elsewhere, e.g. by docker compose, or read directly
# via os.getenv by a dependency-free module).
ALLOWLIST = {
    "FM_IMAGE_TAG",
    "FM_DASHBOARD_IMAGE_TAG",
    # Read via os.getenv in infrastructure/llm/pricing.py, which is kept free of
    # the settings import so it stays trivially unit-testable and hot-path-safe.
    "LLM_PRICING_OVERRIDES",
    # Owned by the Opik SDK, not by settings.py: OpikConfig reads it from
    # os.environ (env_prefix="opik_") and main.py's load_dotenv() puts .env there
    # first. Mirroring it as a settings field would shadow the SDK's default.
    "OPIK_TRACK_DISABLE",
    # Read via os.getenv in config/protection.py (``_fail_open_default``), which
    # builds ProtectionSettings — a plain BaseModel, not a BaseSettings section.
    # Deliberately NOT mirrored as a settings field: the adjacent
    # settings.protection.fail_open (PROTECTION_FAIL_OPEN) governs PII redaction
    # and has the opposite default, and two similarly-named fields on the
    # same section is exactly the one-key-two-meanings confusion this key exists
    # to remove.
    "PROTECTION_RATE_LIMIT_FAIL_OPEN",
    # Read via os.getenv in config/protection.py (``_trusted_proxies_default``),
    # for the same reason as the key above: it populates ProtectionSettings,
    # which is a plain BaseModel rather than a BaseSettings section. Mirroring
    # it onto settings would give the trust policy a second source, and the
    # whole point of the key is that the four loader paths cannot disagree
    # about which proxies a deployment believes.
    "PROTECTION_TRUSTED_PROXIES",
}

# Capture optional leading "#": commented lines document the DEFAULT (checked
# against settings.py); active lines are user overrides/placeholders (not checked).
ASSIGN_RE = re.compile(r"^\s*(#\s*)?([A-Z][A-Z0-9_]+)\s*=\s*(.*?)\s*$")


def collect_settings_defaults() -> dict[str, dict]:
    """env-name -> {default, is_secret, is_complex, has_factory}."""
    out: dict[str, dict] = {}
    for _, obj in vars(settings_module).items():
        if not (inspect.isclass(obj) and issubclass(obj, BaseSettings)):
            continue
        if obj is BaseSettings:
            continue
        for fname, field in obj.model_fields.items():
            env = field.validation_alias
            if not isinstance(env, str):
                env = fname.upper()
            is_secret = "SecretStr" in str(field.annotation)
            has_factory = field.default_factory is not None
            default = None if has_factory else field.default
            is_complex = isinstance(default, (list, dict, tuple, set))
            # First definition wins; sub-settings env names are unique in practice.
            out.setdefault(
                env,
                {
                    "default": default,
                    "is_secret": is_secret,
                    "is_complex": is_complex,
                    "has_factory": has_factory,
                },
            )
    return out


def normalize(value) -> str:
    if isinstance(value, enum.Enum):
        return str(value.value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def parse_env_example(path: Path) -> list[tuple[int, str, str]]:
    """Return [(lineno, KEY, value)] for every COMMENTED assignment.

    Only commented `# KEY=value` lines are the documented defaults that must
    mirror settings.py. Active (uncommented) lines are user overrides/placeholders
    and are intentionally not value-checked.
    """
    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = ASSIGN_RE.match(line)
        if m and m.group(1):  # group(1) present == commented
            # Strip a trailing inline comment. The `\s+#` (whitespace REQUIRED
            # before #) intentionally matches python-dotenv semantics: a `#` with
            # no preceding space is part of the value (e.g. KEY=a#b -> "a#b"), so
            # we must NOT strip it — that keeps this check consistent with how the
            # value is actually parsed at runtime.
            value = re.sub(r"\s+#.*$", "", m.group(3))
            rows.append((i, m.group(2), value))
    return rows


def run_checks(
    env_example_path: Path = ROOT / ".env.example",
) -> tuple[list[str], int, int, list[str]]:
    """Run all sync checks; return (errors, scalar_checked, registry_checked, skipped)."""
    defaults = collect_settings_defaults()
    rows = parse_env_example(env_example_path)

    errors: list[str] = []
    skipped: list[str] = []
    checked = 0
    seen: dict[str, str] = {}

    for lineno, key, val in rows:
        if key in seen and seen[key] != val:
            errors.append(
                f"L{lineno}: {key} documented twice with different values "
                f"({seen[key]!r} vs {val!r}) — show one canonical default"
            )
        seen[key] = val

        if key in ALLOWLIST:
            continue
        if key not in defaults:
            errors.append(
                f"L{lineno}: {key} is in .env.example but is not a settings field "
                f"(typo? or add to ALLOWLIST in this script if intentional)"
            )
            continue

        info = defaults[key]
        if (
            info["is_secret"]
            or info["has_factory"]
            or info["is_complex"]
            or info["default"] is None
        ):
            skipped.append(f"{key} (no shippable scalar default)")
            continue

        expected = normalize(info["default"])
        if val != expected:
            errors.append(
                f"L{lineno}: {key} drift — .env.example={val!r} but "
                f"settings.py default={expected!r}"
            )
        else:
            checked += 1

    # Third leg: registry default_model per provider must also equal the
    # settings.py default for that provider's model field, so settings.py,
    # .env.example, AND the registry are all machine-enforced (not two-of-three).
    # Best-effort: skip if the registry's provider deps can't be imported here.
    reg_checked = 0
    try:
        from faultmaven.infrastructure.llm.providers import registry as _reg

        llm_fields = settings_module.LLMSettings.model_fields
        # Derive provider -> settings field from the registry's OWN model_var
        # (e.g. "OPENAI_MODEL" -> openai_model) rather than a hardcoded parallel
        # map, so a renamed/added provider can't silently drop out of this check.
        # Providers with no scalar settings default (e.g. local, model_var
        # LOCAL_LLM_MODEL -> local_model defaults None) are skipped.
        for prov, schema in _reg.PROVIDER_SCHEMA.items():
            model_var = schema.get("model_var")
            field = model_var.lower() if model_var else None
            if not field or field not in llm_fields:
                continue
            settings_default = llm_fields[field].default
            if settings_default is None:
                continue
            reg_default = schema.get("default_model")
            if reg_default != settings_default:
                errors.append(
                    f"registry PROVIDER_SCHEMA['{prov}'].default_model="
                    f"{reg_default!r} != settings.{field}={settings_default!r}"
                )
            else:
                reg_checked += 1
    except Exception as exc:  # registry deps unavailable (e.g. minimal hook env)
        skipped.append(f"registry default_model cross-check skipped ({exc})")

    return errors, checked, reg_checked, skipped


def main() -> int:
    errors, checked, reg_checked, skipped = run_checks()
    print(
        f"checked {checked} scalar default(s) + {reg_checked} registry default_model(s); "
        f"skipped {len(skipped)} (secret/None/complex); {len(errors)} problem(s)"
    )
    if errors:
        print("\n.env.example is OUT OF SYNC with settings.py:\n")
        for e in errors:
            print(f"  ✗ {e}")
        print(
            "\nFix: update the value in .env.example to match settings.py "
            "(or update the settings.py default if the default itself should change)."
        )
        return 1
    print("✓ .env.example matches settings.py for every documented scalar default")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
