"""Inline runbook validation and quality scoring (v4 causal-chain schema).

Replicated from faultmaven-kb-toolkit to avoid cross-repo dependency. Each
`### Cause` declares one ROOT with sub-fields **Statement** / optional **Chain** /
**Indicators** / quadrant-tagged **Interventions**.

Cause validation is anchored on the SHARED parse grammar (`runbook_grammar`) — the
same section, heading, and sub-field regexes the toolkit parser
and the upstream pack builder consume — so a draft this gate PASSES is exactly one
the toolkit parser can parse into per-Cause records. The gate can no longer
be looser than the parser it fronts.

The cause-level ERRORS mirror the KB Toolkit's `RunbookValidator`
(`kb_toolkit/core/validator.py`): strict `### Cause X:` heading; unique letters;
Cause Z reserved for the `[Default]` fallback; exactly one fallback; a parseable
Chain when present; quadrant-tagged Interventions; token-anchored Indicators whose
`[Step N]` references resolve. Only the message-oriented MISSING-vs-EMPTY wording is
validator-private. Behavioral parity is guarded by the identical test cases in each
repo's suite (the repos cannot import one another).
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from faultmaven.exceptions import ValidationException
from faultmaven.modules.knowledge.domain.models.conversion import (
    QualityScore,
    ValidationResult,
)
from faultmaven.modules.knowledge.domain.services.cause_grammar import (
    FALLBACK_CAUSE_LETTER,
    FALLBACK_INDICATOR_TOKEN,
    INTERVENTION_QUADRANTS,
    LEGACY_V3_CAUSE_SUBFIELDS,
    OPTIONAL_CAUSE_SUBFIELDS,
    QUADRANT_ALTERNATION,
    REQUIRED_CAUSE_SUBFIELDS,
)

# Same-package sibling (knowledge domain service): the chunk bounds and the split
# boundary are imported, never re-declared, so the one-cause-per-chunk gate can
# never drift from the chunker that actually splits the runbook for retrieval.
from faultmaven.modules.knowledge.domain.services.content_chunker import (
    HEADER_SPLIT_BOUNDARY_RE,
    ContentChunker,
)

# Shared v4 parse grammar — the SAME regexes + sub-field parser the upstream
# pack builder consumes. Anchoring
# the validator's cause enumeration here is what keeps the gate from being looser
# than the parser it fronts (a draft the gate passes must be one the toolkit can
# parse into the exact per-Cause records the toolkit emits).
from faultmaven.modules.knowledge.domain.services.runbook_grammar import (
    CAUSE_HEADING_RE,
    CHAIN_RUNG_RE,
    CONVERGES_REF,
    INDICATOR_TOKEN_RE,
    INTERVENTION_RE,
    STEP_HEADING_RE,
    STEP_REF_RE,
    causes_section,
    iter_cause_blocks,
    parse_cause_subfields,
)

# =============================================================================
# Security hazard detection (same shape as kb-toolkit's validator). Hazards
# BLOCK (errors). A secret value that is WHOLLY a placeholder / template-or-shell
# var stays non-blocking (warning). NOTE: duplicated cross-repo; Phase 1 extracts
# a shared module.
# =============================================================================

# Anchored: the WHOLE value must BE a placeholder form, so a real secret that
# merely contains '$' or 'example' (e.g. ``P@$$w0rd``, ``...EXAMPLE``) still blocks.
_PLACEHOLDER_VALUE_RE = re.compile(
    r"""^\s*(?:
        <[^>]*>              # <repl_password>
      | \$\{[^}]*\}          # ${VAR}
      | \{\{[^}]*\}\}        # {{ password }}
      | \$[A-Za-z_]\w*[})]?  # $HOME, $TOKEN (opt. trailing } / ) from shell/awk)
      | \$\d+[})]?           # $3, $3} (positional, opt. shell/awk closer)
      | your[-_][\w-]*       # your-key
      | change[-_]?me
      | example[\w-]*
      | placeholder[\w-]*
      | redacted
      | x{3,}
      | todo
      | \.\.\.
      | \*+
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

_SECRET_PATTERNS = [
    (
        re.compile(r"password\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE),
        "Potential hardcoded password",
    ),
    (
        re.compile(r"api[_-]?key\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE),
        "Potential hardcoded API key",
    ),
    (
        re.compile(r"secret\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE),
        "Potential hardcoded secret",
    ),
    (
        re.compile(r"token\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE),
        "Potential hardcoded token",
    ),
]

_RM_SEGMENT_RE = re.compile(r"(?im)\brm\b([^\n|;&]*)")
_FORK_BOMB_RE = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&?\s*\}\s*;\s*:")
_DD_DEVICE_RE = re.compile(
    r"\bdd\b[^\n|;&]*\bof=/dev/(?:sd|nvme|hd|vd|xvd|mapper|disk|loop|md)\w*",
    re.IGNORECASE,
)


def _value_is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER_VALUE_RE.match(value))


def _rm_target_is_catastrophic(target: str) -> bool:
    """True for the filesystem root, a whole top-level dir, home, or a var — NOT a
    scoped deeper path like /var/lib/app/* (legitimate cleanup)."""
    target = target.strip()
    if target in ("~", "/", "/*", "//"):
        return True
    if re.fullmatch(r"\$\{?\w+\}?", target):  # $HOME, ${HOME}
        return True
    return bool(re.fullmatch(r"/+([\w.-]+)?/?\*?", target))  # /, /etc, /etc/, /etc/*


# --- Cause-Statement invariants (the symptom-level match surface; #545) -------
#
# Post-#545 the cause Statement (not the per-rung Indicators) is the load-bearing
# match surface, matched holistically per cause. Two authoring invariants follow.
# Bias is hard toward WARN: a mis-authored Statement only degrades the matcher to
# abstention (verdict 'multiple') or a capped-0.5 CANDIDATE prior that can't
# conclude without real evidence (M5 + never-VALIDATED) — it cannot cause an
# incorrect conclusion. So a false BLOCK is the only real harm; we block ONLY
# unambiguous mechanical tells (verified never to fire on the 91 shipped runbooks)
# and warn on the rest. True "is this symptom-level prose" judgment is the
# build-time generator's job (a separate LLM rule), not this lexical gate.
#
# The block below (constants + helpers + check_cause_statement_invariants) is
# mirrored BYTE-IDENTICAL in the other repo (faultmaven runbook_validator.py
# <-> kb_toolkit/core/validator.py) — the repos can't import each other. The
# identical behavioral test cases in each repo's suite are the drift backstop;
# keep both the code and those cases in sync.

_STEP_MARKER_RE = re.compile(r"\[step\s*\d+\]", re.IGNORECASE)
_SIBLING_NEAR_DUP_JACCARD = 0.6


def _norm_statement(s: str) -> str:
    # Same word sequence, ignoring case / punctuation / whitespace — so two
    # Statements that differ only by a trailing period count as identical, and an
    # all-punctuation Statement normalizes to "" (skipped, never a false dup).
    return " ".join(re.findall(r"[a-z0-9]+", s.lower()))


def _statement_jaccard(a: str, b: str) -> float:
    ta = set(re.findall(r"[a-z0-9]+", a.lower()))
    tb = set(re.findall(r"[a-z0-9]+", b.lower()))
    return len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0


def check_cause_statement_invariants(
    statements: List[tuple],
) -> tuple[List[str], List[str]]:
    """Invariants on NON-FALLBACK cause Statements (the match surface).

    Args:
        statements: list of ``(cause_letter, statement)`` for non-fallback causes.

    Returns ``(errors, warnings)``:
      (i)  BLOCK a Statement carrying an operator-step marker (``[Step N]``) — an
           Indicator leaking into the symptom-level match surface.
      (ii) BLOCK exact-duplicate sibling Statements (one error per duplicate set);
           WARN on near-duplicates (lexical Jaccard >= 0.6) — sibling causes must
           be mutually discriminative (MECE) or holistic matching returns
           'multiple' and the matcher abstains.
    """
    errors: List[str] = []
    warnings: List[str] = []
    for letter, stmt in statements:
        if _STEP_MARKER_RE.search(stmt or ""):
            errors.append(
                f"Cause {letter}: Statement contains an operator-step marker "
                f"([Step N]). The Statement is the symptom-level match surface — "
                f"describe the observable condition; keep step references in Indicators."
            )
    # Exact duplicates: group by normalized form, one error per duplicate set (not
    # C(n,2) messages). Statements with no alphanumeric content normalize to "" and
    # are skipped so they can't collide as spurious duplicates.
    norms = [(letter, _norm_statement(stmt)) for letter, stmt in statements]
    groups: Dict[str, List[str]] = {}
    for letter, norm in norms:
        if norm:
            groups.setdefault(norm, []).append(letter)
    for letters in groups.values():
        if len(letters) > 1:
            errors.append(
                f"Causes {', '.join(letters)} have identical Statements; sibling "
                f"causes must be mutually discriminative (MECE) or the matcher abstains."
            )
    # Near-duplicates (high lexical overlap but not identical): pairwise warn.
    for i in range(len(statements)):
        li, si = statements[i]
        if not norms[i][1]:
            continue
        for j in range(i + 1, len(statements)):
            lj, sj = statements[j]
            if not norms[j][1] or norms[i][1] == norms[j][1]:
                continue
            if _statement_jaccard(si, sj) >= _SIBLING_NEAR_DUP_JACCARD:
                warnings.append(
                    f"Causes {li} and {lj} have near-duplicate Statements (lexical "
                    f"overlap >= {_SIBLING_NEAR_DUP_JACCARD:.0%}); make them "
                    f"discriminative or holistic matching may return 'multiple'."
                )
    return errors, warnings


def find_security_hazards(content: str) -> tuple[list[str], list[str]]:
    """Return (blocking_errors, non_blocking_warnings) for security hazards.

    Hardcoded credentials and destructive commands are errors; a secret whose
    value is wholly a placeholder/template-var is a warning. One message per
    pattern (deduped), so N identical matches don't produce N messages.
    """
    errors: List[str] = []
    warnings: List[str] = []

    for rx, message in _SECRET_PATTERNS:
        values = [m.group(1) for m in rx.finditer(content)]
        if not values:
            continue
        if any(not _value_is_placeholder(v) for v in values):
            errors.append(f"Security: {message} (hardcoded credential)")
        else:
            warnings.append(f"Security: {message} (placeholder — not blocking)")

    for seg in _RM_SEGMENT_RE.finditer(content):
        args = seg.group(1)
        flags = " " + " ".join(re.findall(r"(?<!\S)-{1,2}[A-Za-z][A-Za-z-]*", args))
        recursive = re.search(r"-[A-Za-z]*r|--recursive", flags, re.IGNORECASE)
        force = re.search(r"-[A-Za-z]*f|--force", flags, re.IGNORECASE)
        if not (recursive and force):
            continue
        positionals = [t for t in args.split() if not t.startswith("-")]
        if positionals and _rm_target_is_catastrophic(positionals[0]):
            errors.append(
                "Security: Dangerous command: recursive force-remove of a "
                "root/top-level/home path (rm -rf)"
            )
            break

    if _FORK_BOMB_RE.search(content):
        errors.append("Security: Dangerous: fork bomb")
    if _DD_DEVICE_RE.search(content):
        errors.append("Security: Potentially destructive: dd writing to a block device")

    return errors, warnings


# =============================================================================
# Validation Constants (aligned with KB Toolkit config defaults)
# =============================================================================

REQUIRED_METADATA = [
    "id",
    "title",
    "domain",
    "service",
    "symptom_class",
    "severity",
    "scope",
    "version",
    "last_updated",
    "verified_by",
    "status",
]

REQUIRED_SECTIONS = [
    "Symptom Recognition",
    "Applicability",
    "Diagnostic Steps",
    "Causes",
    "Prevention",
    "Sources",
]

VALID_DOMAINS = [
    "database",
    "networking",
    "compute",
    "application",
    "security",
    "storage",
    "messaging",
]

VALID_SCOPES = ["global", "team", "personal"]

VALID_SEVERITIES = ["critical", "high", "medium", "low", "info"]

VALID_DIFFICULTIES = ["beginner", "intermediate", "advanced", "expert"]

VALID_STATUSES = ["draft", "in-review", "verified", "stale", "deprecated"]

# Controlled vocabulary for `symptom_class` — the failure-mode taxonomy. Mirrors
# the kb-toolkit producer side (``ValidationConfig.valid_symptom_classes``,
# config.py) exactly: same 16 curated values, same order. Like ``VALID_DOMAINS``,
# this is a hand-maintained copy — the repos can't import each other — so grow it
# HERE and in kb-toolkit in lock-step with the taxonomy design rule
# (runbook-content-architecture.md §Taxonomy-Design-Rules), never by loosening the
# gate. An off-vocabulary value is a hard error: the author either extends the
# vocabulary deliberately or moves a long-tail symptom into the free-text `tags`
# escape valve. `service` stays free-text (technologies proliferate faster than a
# curated list can track).
VALID_SYMPTOM_CLASSES = [
    "auth_failure",
    "connection_refused",
    "cpu_saturation",
    "crash_loop",
    "data_loss",
    "deployment_failure",
    "disk_full",
    "image_pull_failure",
    "latency",
    "node_failure",
    "oom",
    "replication_lag",
    "scheduling_failure",
    "service_unavailable",
    "throughput_degradation",
    "timeout",
]

MAX_TITLE_LENGTH = 100
MIN_CONTENT_LENGTH = 500
MAX_TAG_COUNT = 10
# Hard limit on a Cause **Statement** (the match surface). Mirrors the kb-toolkit
# generator/validator (``config.validation.cause_statement_max_chars``) and the
# Template Compliance Rules (runbook-content-architecture.md §3, rule 5). Kept a
# local literal here — it is a scalar limit, NOT part of the cross-repo authoring
# VOCABULARY guarded by ``cause_grammar``/``check_vocab_cross_repo.py``.
MAX_CAUSE_STATEMENT_LENGTH = 300
# Hard limit on a single Chain **rung** statement. Mirrors the kb-toolkit
# validator (``config.validation.rung_statement_max_chars``). A local scalar
# literal, like the Statement limit above — not part of the cross-repo VOCABULARY.
MAX_CHAIN_RUNG_LENGTH = 300


# =============================================================================
# Cause-block parsing — anchored on the SHARED grammar (``runbook_grammar``)
#
# The section scope (``CAUSES_SECTION_RE``), the ``### Cause X:`` heading
# (``CAUSE_HEADING_RE``), and the sub-field split (``parse_cause_subfields``) are
# the EXACT ones the upstream pack builder use. So a draft the gate
# passes is one the toolkit parser can parse into the same per-Cause
# records — the gate can no longer accept a Cause shape the toolkit parser silently
# drops (a stricter heading, a stray-bold-truncated Statement, an out-of-section
# heading that the gate counts but the parser does not).
# =============================================================================

# A near-miss cause heading — anything that reads like a Cause heading but is not
# the strict ``### Cause X: <name>`` form. Used to flag (not silently drop) an
# ``#### Cause`` / ``### Cause AA`` / ``### Cause A :`` an author likely intended
# as a Cause. Deliberately loose; ``Cause\b`` will NOT match the ``## Causes``
# section header itself ("Causes" has no word boundary after "Cause").
_LOOSE_CAUSE_HEADING_RE = re.compile(r"^[ \t]*#{2,}[ \t]*Cause\b.*$", re.MULTILINE)

# Fenced code block (```...```), stripped before the loose malformed-heading scan
# so an illustrative heading inside a command example is not a false near-miss.
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

# Sub-field boundary set — the schema's required + optional labels, identical to
# the toolkit's, so a Statement the gate length-checks is byte-for-byte the one
# the pack builder parses.
_CAUSE_SUBFIELDS: List[str] = list(REQUIRED_CAUSE_SUBFIELDS) + list(
    OPTIONAL_CAUSE_SUBFIELDS
)


def _causes_section_body(content: str) -> str:
    """Comment-MASKED body of the ``## Causes`` H2 section, or "".

    Uses the shared ``causes_section`` so the gate scopes causes to exactly the
    span the toolkit parser does: a ``### Cause``-style heading in ANOTHER section stays
    out of cause validation, and the LAST cause's block cannot bleed into
    ``## Prevention`` / ``## Sources`` (which would let a whole-body sub-field scan
    find a required label in a trailing section and mask a genuinely-missing one).

    Masked, and the section itself located in the masked document (#1241): a
    comment opening BEFORE the ``## Causes`` heading otherwise leaves the whole
    section live, which measured as a full gate bypass.
    """
    return causes_section(content)[1]


def _section_body(content: str, heading: str) -> Optional[str]:
    """Body of an arbitrary ``## <heading>`` H2 section (up to the next H2 or EOF),
    or ``None`` if the section is absent. Used to resolve ``[Step N]`` references
    against ``## Diagnostic Steps``."""
    m = re.search(
        rf"^##\s+{re.escape(heading)}\s*\n(.*?)(?=^##\s+|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    return m.group(1) if m else None


def _cause_fields(body: str) -> Dict[str, str]:
    """Parse one Cause block's ``**Field:**`` sub-fields with the shared parser.

    Returns ``{label: value}`` for every label PRESENT in the block (value stripped;
    ``""`` when present-but-empty). A missing label is simply absent from the dict —
    so the caller keeps the message-oriented MISSING-vs-EMPTY distinction while the
    field boundaries match the toolkit parser exactly (splitting only on the four schema
    labels, so a stray ``**Note:**`` no longer truncates a Statement before the
    length gate)."""
    return parse_cause_subfields(body, _CAUSE_SUBFIELDS)


def _iter_cause_blocks(content: str, include_heading: bool = False):
    """Yield ``(letter, name, text)`` for each strict ``### Cause X:`` block, from
    the SHARED walk (``runbook_grammar.iter_cause_blocks``) the toolkit parser uses —
    so the gate can never accept a Cause shape the toolkit parser silently drops.

    By default ``text`` is the comment-MASKED post-heading BODY (what the
    sub-field parser consumes); with ``include_heading=True`` it is the RAW span
    ``ContentChunker`` sees — the heading line THROUGH the block terminus,
    comments included — so a length measured on it matches the chunker's
    per-section length. A heading that is not the strict form (``#### Cause``,
    ``### Cause AA``, ``### Cause A :``) is NOT yielded here — the toolkit parser drops
    it too; it is surfaced separately by ``_flag_malformed_cause_headings``."""
    for cause in iter_cause_blocks(content):
        yield (
            cause.letter,
            cause.name,
            cause.raw_block if include_heading else cause.body,
        )


def _cause_is_fallback(fields: Dict[str, str]) -> bool:
    """The fallback Cause is the one whose **Indicators** carry ``[Default]`` — the
    SAME key the toolkit parser uses to set ``is_fallback_cause`` (``[Default]`` only,
    NOT the letter Z). Keying on ``[Default]`` alone is what lets the validator
    catch a ``### Cause Z:`` that OMITS ``[Default]`` (which the toolkit parser would seed
    as a real candidate root, not a fallback)."""
    return FALLBACK_INDICATOR_TOKEN in fields.get("Indicators", "")


# =============================================================================
# Runbook Validator
# =============================================================================


class RunbookValidator:
    """Validates runbook markdown files against quality standards."""

    def validate_file(self, file_path: Path) -> ValidationResult:
        """Validate a runbook file on disk."""
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            return ValidationResult(passed=False, errors=[f"Failed to read file: {e}"])

        return self.validate_content(content)

    def validate_content(self, content: str) -> ValidationResult:
        """Validate runbook markdown content."""
        errors: List[str] = []
        warnings: List[str] = []

        # Gate 1: YAML frontmatter metadata
        metadata = self._extract_metadata(content)
        if metadata is None:
            errors.append("No YAML frontmatter found")
        else:
            self._validate_metadata(metadata, errors, warnings)

        # Gate 2: Structural linting
        self._validate_structure(content, errors, warnings)

        # Gate 2a: per-Cause required sub-fields + Statement length (per-cause
        # ERROR — parity with the kb-toolkit generator/validator)
        self._validate_cause_subfields(content, errors, warnings)

        # Gate 2b: per-Cause Statement invariants (the match surface; #545)
        self._validate_cause_statements(content, errors, warnings)

        # Gate 2c: per-Cause graph shape (heading form, duplicate/reserved letters,
        # exactly-one fallback, parseable Chain, quadrant-tagged Interventions,
        # token-anchored Indicators) — ERROR parity with the kb-toolkit validator so
        # a passing draft parses into the same per-Cause records the toolkit emits.
        self._validate_cause_graph(content, errors, warnings)

        # Gate 2d: per-Cause retrieval-chunk guard (bounds/boundary imported FROM
        # the chunker) — no ### Cause block is CUT by ContentChunker (oversize
        # line-split, embedded heading-boundary line) or whole-MERGED with a
        # neighboring Cause (undersize).
        self._validate_cause_chunk_boundaries(content, errors, warnings)

        # Content quality checks
        self._validate_quality(content, warnings)

        # Security checks
        self._validate_security(content, errors, warnings)

        return ValidationResult(
            passed=len(errors) == 0, errors=errors, warnings=warnings
        )

    def _extract_metadata(self, content: str) -> Optional[Dict[str, Any]]:
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not match:
            return None
        try:
            return yaml.safe_load(match.group(1))
        except yaml.YAMLError as e:
            return None

    def _validate_metadata(
        self, metadata: Dict[str, Any], errors: List[str], warnings: List[str]
    ) -> None:
        # Required fields
        for field in REQUIRED_METADATA:
            if field not in metadata:
                errors.append(f"Missing required metadata field: {field}")
            elif metadata[field] is None or (
                isinstance(metadata[field], str) and not metadata[field].strip()
            ):
                # verified_by is allowed to be empty for drafts
                if field == "verified_by":
                    continue
                errors.append(f"Empty required metadata field: {field}")

        # ID format
        if "id" in metadata and isinstance(metadata["id"], str):
            if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", metadata["id"]):
                errors.append(
                    f"ID must be kebab-case (lowercase, hyphen-separated): {metadata['id']}"
                )

        # Title length
        if "title" in metadata and isinstance(metadata["title"], str):
            if len(metadata["title"]) > MAX_TITLE_LENGTH:
                errors.append(
                    f"Title exceeds maximum length ({len(metadata['title'])} > {MAX_TITLE_LENGTH})"
                )
            if len(metadata["title"]) < 10:
                warnings.append("Title is very short (< 10 characters)")

        # Severity vocabulary
        if "severity" in metadata and isinstance(metadata["severity"], str):
            if metadata["severity"].lower() not in VALID_SEVERITIES:
                errors.append(
                    f"Invalid severity '{metadata['severity']}'. "
                    f"Must be one of: {', '.join(VALID_SEVERITIES)}"
                )

        # Domain vocabulary
        if "domain" in metadata and isinstance(metadata["domain"], str):
            if metadata["domain"].lower() not in VALID_DOMAINS:
                errors.append(
                    f"Invalid domain '{metadata['domain']}'. "
                    f"Must be one of: {', '.join(VALID_DOMAINS)}"
                )

        # Scope vocabulary
        if "scope" in metadata and isinstance(metadata["scope"], str):
            if metadata["scope"].lower() not in VALID_SCOPES:
                errors.append(
                    f"Invalid scope '{metadata['scope']}'. "
                    f"Must be one of: {', '.join(VALID_SCOPES)}"
                )

        # Status vocabulary
        if "status" in metadata and isinstance(metadata["status"], str):
            if metadata["status"].lower() not in VALID_STATUSES:
                errors.append(
                    f"Invalid status '{metadata['status']}'. "
                    f"Must be one of: {', '.join(VALID_STATUSES)}"
                )

        # Tags
        if "tags" in metadata:
            tags = metadata["tags"]
            if isinstance(tags, list):
                if len(tags) > MAX_TAG_COUNT:
                    warnings.append(f"Too many tags ({len(tags)} > {MAX_TAG_COUNT})")
                for tag in tags:
                    if isinstance(tag, str) and not re.match(r"^[a-z0-9-]+$", tag):
                        errors.append(
                            f"Tag must be lowercase alphanumeric with hyphens: {tag}"
                        )

        # Symptom class — controlled vocabulary (faithfully mirrors kb-toolkit
        # RunbookValidator._validate_symptom_class): shape, then format, then a
        # hard error on any value outside VALID_SYMPTOM_CLASSES. The vocab check
        # subsumes the old format-only gate — every off-vocab value (including a
        # hyphenated one like `throughput-degradation`) is now rejected. The shape
        # check matters: a YAML scalar (``symptom_class: unknown``) or a non-string
        # item would otherwise slip past a list-only gate and persist unchecked —
        # the exact drift this vocabulary exists to prevent, through the front door.
        if "symptom_class" in metadata:
            sc = metadata["symptom_class"]
            if not isinstance(sc, list):
                errors.append(f"symptom_class must be a list, got {type(sc).__name__}")
            elif not sc:
                warnings.append("No symptom classes specified")
            else:
                for item in sc:
                    if not isinstance(item, str):
                        errors.append(f"symptom_class items must be strings: {item!r}")
                    elif not re.match(r"^[a-z0-9_-]+$", item):
                        errors.append(
                            f"symptom_class must be lowercase with hyphens/underscores: {item}"
                        )
                    elif item not in VALID_SYMPTOM_CLASSES:
                        errors.append(
                            f"Invalid symptom_class '{item}'. Must be one of the controlled "
                            f"vocabulary ({', '.join(VALID_SYMPTOM_CLASSES)}), or move it to "
                            f"`tags` if it is a long-tail symptom."
                        )

        # Version
        if "version" in metadata and isinstance(metadata["version"], str):
            if not re.match(r"^\d+\.\d+\.\d+$", metadata["version"]):
                errors.append(
                    f"Version must be semantic version (X.Y.Z): {metadata['version']}"
                )

    def _validate_structure(
        self, content: str, errors: List[str], warnings: List[str]
    ) -> None:
        for section in REQUIRED_SECTIONS:
            # Exact-anchored (``^## Section$``), matching kb-toolkit and the
            # ``_CAUSES_SECTION_RE`` used for per-Cause scoping. A prefix match
            # (``## Causes (RCA)``, ``### Causes``) would otherwise pass this gate
            # yet be skipped by the exact-anchored section-body parse — silently
            # dropping every per-Cause sub-field ERROR for that runbook.
            pattern = rf"^##[ \t]+{re.escape(section)}[ \t]*$"
            if not re.search(pattern, content, re.MULTILINE):
                errors.append(f"Missing required section: {section}")

        # ## Causes must have at least one ### Cause subsection. Scanned
        # SECTION-SCOPED with the shared strict ``CAUSE_HEADING_RE`` (not a loose
        # whole-content scan) so an example ``### Cause`` heading in another section
        # cannot satisfy this gate while the toolkit parser parses zero causes. Comments
        # are masked for the same reason (#1241): a section holding only a
        # commented-out example Cause parses to zero causes, so it must fail here.
        if re.search(r"^##[ \t]+Causes[ \t]*$", content, re.MULTILINE):
            if not iter_cause_blocks(content):
                errors.append(
                    "## Causes section must contain at least one ### Cause subsection"
                )
            # NOTE: the fallback-Cause requirement is now an ERROR in
            # _validate_cause_graph (Gate 2c), not a warning here.
            # v4: each ### Cause carries Statement / Indicators / Interventions
            # (Chain optional); interventions are quadrant-tagged. Per-Cause
            # presence/emptiness is enforced (as ERRORs) in _validate_cause_subfields
            # (Gate 2a); here only the document-level quadrant + legacy-v3 tells.
            if re.search(r"\*\*Interventions:\*\*", content) and not re.search(
                rf"\*\*({QUADRANT_ALTERNATION})\*\*", content
            ):
                warnings.append(
                    "Interventions present but no quadrant tag "
                    f"({QUADRANT_ALTERNATION.replace('|', ' / ')})"
                )
            # v4 has no AND-sets in authored runbooks; flag legacy v3 sub-fields.
            _legacy = "|".join(LEGACY_V3_CAUSE_SUBFIELDS)
            if re.search(rf"\*\*({_legacy}):\*\*", content):
                warnings.append(
                    f"Found v3 Cause sub-field(s) ({_legacy.replace('|', '/')}) — "
                    "v4 uses Statement / Chain / Indicators / quadrant-tagged Interventions"
                )

    def _validate_cause_subfields(
        self, content: str, errors: List[str], warnings: List[str]
    ) -> None:
        """Per-Cause required sub-fields + Statement length (per-cause **ERROR**).

        Parity with the kb-toolkit generator/validator: every ``### Cause`` — the
        fallback included — must carry a non-empty ``**Statement:**`` /
        ``**Indicators:**`` / ``**Interventions:**`` (``**Chain:**`` is optional),
        and the Statement must be within the hard char limit. A missing sub-field
        and a present-but-empty one are distinguished. Coarse, document-level parse.
        Previously these were a single document-level WARNING (a Cause missing
        ``Interventions`` passed if any *other* Cause had one) — now blocked per
        Cause, matching the generation path that authors the corpus.
        """
        for letter, _name, body in _iter_cause_blocks(content):
            label = f"Cause {letter}"
            fields = _cause_fields(body)
            for sub in REQUIRED_CAUSE_SUBFIELDS:
                if sub not in fields:
                    errors.append(f"{label}: missing required **{sub}:** sub-field")
                    continue
                value = fields[sub]
                if not value:
                    errors.append(f"{label}: **{sub}:** sub-field is empty")
                elif sub == "Statement" and len(value) > MAX_CAUSE_STATEMENT_LENGTH:
                    errors.append(
                        f"{label}: Statement is {len(value)} chars "
                        f"(>{MAX_CAUSE_STATEMENT_LENGTH})"
                    )

    def _validate_cause_statements(
        self, content: str, errors: List[str], warnings: List[str]
    ) -> None:
        """Match-surface invariants (#545) on NON-FALLBACK Cause Statements.

        Drop the fallback Cause (``[Default]`` in its *Indicators* — the SAME key
        the toolkit parser uses, NOT the letter Z, so a mis-lettered ``### Cause Z:``
        without ``[Default]`` is still checked), collect each remaining Cause's
        non-empty ``**Statement:**``, and run the shared
        ``check_cause_statement_invariants`` (no ``[Step N]`` leak; siblings mutually
        discriminative). Missing/empty Statements are owned by
        ``_validate_cause_subfields`` (Gate 2a); an empty one is simply skipped here
        (a non-empty Statement is needed for the check).
        """
        statements: List[tuple] = []
        for letter, _name, body in _iter_cause_blocks(content):
            fields = _cause_fields(body)
            if _cause_is_fallback(fields):
                continue  # fallback Cause — not a match surface
            stmt = fields.get("Statement", "")
            if stmt:
                statements.append((letter, stmt))
        errs, warns = check_cause_statement_invariants(statements)
        errors.extend(errs)
        warnings.extend(warns)

    def _validate_cause_graph(
        self, content: str, errors: List[str], warnings: List[str]
    ) -> None:
        """Per-Cause graph-shape checks (Gate 2c) — ERROR parity with kb-toolkit.

        Ports the structural errors the backend gate was missing, each guarding a
        way a runbook could pass validation yet parse into wrong/zero causes for the
        toolkit parser: a malformed heading the toolkit parser drops; a duplicate letter; a
        real Cause on the reserved letter Z (which the toolkit parser would seed as a
        candidate root); a missing/duplicate ``[Default]`` fallback; a Chain present
        but unparseable; an Intervention with no/invalid quadrant tag; an Indicator
        entry with no ``[Step N]``/``[Symptom]``/``[Default]`` token or an unresolved
        ``[Step N]``. Coarse, section-scoped parse (same span as the toolkit parser).
        """
        body = _causes_section_body(content)
        if not body:
            return  # a missing ## Causes section is flagged by _validate_structure

        # Near-miss headings the toolkit parser silently drops (surfaced, not skipped).
        self._flag_malformed_cause_headings(body, errors)

        causes = list(_iter_cause_blocks(content))
        if not causes:
            return  # "no ### Cause subsection" is flagged by _validate_structure

        diagnostic_steps = self._collect_step_numbers(content)
        letters_seen: Dict[str, str] = {}
        real_count = 0
        fallback_count = 0
        for letter, name, cbody in causes:
            label = f"Cause {letter}"
            if letter in letters_seen:
                errors.append(
                    f"Duplicate Cause letter '{letter}' "
                    f"(already used for: {letters_seen[letter]})"
                )
            letters_seen[letter] = name

            fields = _cause_fields(cbody)
            indicator_text = fields.get("Indicators", "")
            is_fallback = _cause_is_fallback(fields)
            if is_fallback:
                fallback_count += 1
                if letter.upper() != FALLBACK_CAUSE_LETTER:
                    warnings.append(
                        f"{label}: fallback Cause (uses {FALLBACK_INDICATOR_TOKEN}) "
                        f"should be named 'Cause {FALLBACK_CAUSE_LETTER}' for convention"
                    )
            else:
                real_count += 1
                if letter.upper() == FALLBACK_CAUSE_LETTER:
                    errors.append(
                        f"{label}: Cause {FALLBACK_CAUSE_LETTER} is reserved for the "
                        f"{FALLBACK_INDICATOR_TOKEN} fallback; real Causes must use A-Y"
                    )

            valid_refs = self._validate_chain(
                label, fields.get("Chain", ""), errors, warnings
            )
            self._validate_interventions(
                label, fields.get("Interventions", ""), valid_refs, errors, warnings
            )
            self._validate_indicator_field(
                label, indicator_text, diagnostic_steps, is_fallback, errors, warnings
            )

        if real_count == 0:
            errors.append(
                "## Causes must contain at least one real ### Cause (A-Y) in "
                f"addition to the {FALLBACK_INDICATOR_TOKEN} fallback"
            )
        if fallback_count == 0:
            errors.append(
                "## Causes must contain a fallback Cause whose Indicator includes "
                f"{FALLBACK_INDICATOR_TOKEN} (conventionally "
                f"### Cause {FALLBACK_CAUSE_LETTER}: Unidentified)"
            )
        elif fallback_count > 1:
            errors.append(
                f"## Causes contains {fallback_count} Causes with "
                f"{FALLBACK_INDICATOR_TOKEN}; exactly one fallback is required"
            )

    def _validate_cause_chunk_boundaries(
        self, content: str, errors: List[str], warnings: List[str]
    ) -> None:
        """Per-Cause retrieval-chunk guard (Gate 2d) — **ERROR** on any Cause
        block that would not survive chunking as its own clean retrieval unit:
        one the ``ContentChunker`` would CUT, or one so small the chunker
        whole-MERGES it into a neighboring Cause.

        Two hard properties of a passing Cause: it is never CUT (retrieval never
        returns it split mid-chain or mid-interventions), and it is never so
        small the chunker fuses it whole into a neighbor (two Causes in one
        chunk). The chunker splits the document on markdown heading boundaries,
        then merges any section below ``MIN_CHUNK_CHARS`` into a neighbor and
        line-splits any section above ``MAX_CHUNK_CHARS``. Three authoring shapes
        are blocked, per Cause:

          1. **Oversized** (block > ``MAX_CHUNK_CHARS``): line-split mid-block
             (a CUT).
          2. **Undersized** (block < ``MIN_CHUNK_CHARS``): fused WHOLE into a
             neighboring section — not cut, but two Causes land in one chunk.
          3. **Internal heading boundary**: any body line matching the chunker's
             split pattern (``#{1,4}\\s+\\S`` at line start) — including a bash
             ``# comment`` inside a fenced code block, which the chunker does not
             parse — splits the Cause at that line regardless of size.

        (What the gate deliberately does NOT claim: literal one-Cause-per-chunk.
        The chunker's small-section merge can still FUSE whole in-bounds Causes
        behind a tiny preceding section — e.g. the bare ``## Causes`` heading
        line, a section of its own — into one chunk. Fusion dilutes a chunk but
        never cuts a Cause, and is not preventable from the authoring side; the
        undersize check only removes Causes that would themselves seed a merge.)

        Bounds and the boundary regex are imported FROM the chunker
        (``ContentChunker.MAX_CHUNK_CHARS`` / ``MIN_CHUNK_CHARS`` /
        ``HEADER_SPLIT_BOUNDARY_RE``), so the gate can never drift from the code that
        actually chunks the runbook. The block is measured exactly as the chunker
        sees it: the heading line through the block terminus, ``.strip()``-ed.
        """
        max_chars = ContentChunker.MAX_CHUNK_CHARS
        min_chars = ContentChunker.MIN_CHUNK_CHARS
        for letter, _name, block in _iter_cause_blocks(content, include_heading=True):
            size = len(block.strip())
            if size > max_chars:
                errors.append(
                    f"Cause {letter}: retrieval-chunk oversize — the block is "
                    f"{size} chars (> {max_chars} chunk max), so retrieval would "
                    f"line-split it into multiple chunks, cutting the Cause "
                    f"mid-chain or mid-interventions. Split this failure mode into "
                    f"separate Causes or trim the block."
                )
            elif size < min_chars:
                errors.append(
                    f"Cause {letter}: retrieval-chunk undersize — the block is "
                    f"{size} chars (< {min_chars} chunk min), so retrieval would "
                    f"merge it with a neighboring section, putting two Causes in one "
                    f"chunk. Expand the Cause so it stands alone as a retrieval chunk."
                )
            # Internal heading-boundary lines. The block starts with the ``### Cause``
            # heading (no leading newline within the span), so searching the split
            # boundary finds only lines AFTER it — the extra split points.
            for m in HEADER_SPLIT_BOUNDARY_RE.finditer(block):
                offending = block[m.end() :].split("\n", 1)[0].strip()
                errors.append(
                    f"Cause {letter}: retrieval-chunk split — the line "
                    f"{offending!r} matches a chunk-split boundary (a line starting "
                    f"with '#'–'####' then text) inside the Cause body, so retrieval "
                    f"would split the Cause at this line regardless of size. Indent "
                    f"the line by one space so it no longer starts at column 0 and "
                    f"the boundary pattern (anchored at line start) no longer matches."
                )

    def _flag_malformed_cause_headings(
        self, causes_body: str, errors: List[str]
    ) -> None:
        """ERROR on a heading that reads like a Cause but is not the strict
        ``### Cause X: <name>`` form (``#### Cause``, ``### Cause AA``,
        ``### Cause A :``, lowercase/numeric letter, empty name). The toolkit parser
        matches ONLY the strict form, so any near-miss is silently dropped — this
        converts that silent drop into an actionable error.

        Fenced code blocks are stripped first so an illustrative ``#### Cause`` in
        an Intervention's command example is not mistaken for a malformed heading
        (a false block); a genuine ``### Cause`` in a fence is parsed as a cause by
        the toolkit parser anyway, so it is not a near-miss to flag."""
        # ``causes_body`` arrives comment-MASKED from ``_causes_section_body``,
        # so this must NOT strip comments again — and above all must not DELETE
        # them. Deleting splices the lines either side together, which measurably
        # stopped a heading being a heading: the enumerator dropped the Cause and
        # this guard, the one that exists to turn a silent drop into an error,
        # could not see it either (#1241). Fences are still removed, for their
        # own reason: an illustrative ``#### Cause`` in a command example.
        scan = _CODE_FENCE_RE.sub("", causes_body)
        for m in _LOOSE_CAUSE_HEADING_RE.finditer(scan):
            # Compare with LEADING whitespace preserved: the strict grammar (and the
            # toolkit parser) anchor the heading at column 0, so an INDENTED ``### Cause``
            # is a near-miss the toolkit parser drops too — stripping first would hide it.
            raw = m.group(0).rstrip()
            if not CAUSE_HEADING_RE.match(raw):
                errors.append(
                    f"Malformed Cause heading {raw.strip()!r}: expected "
                    f"'### Cause X: <name>' (H3 '###', a single uppercase letter A-Z, "
                    f"then ':' immediately after the letter, then a non-empty name). "
                    f"The toolkit parser silently drops any other form."
                )

    def _validate_chain(
        self,
        label: str,
        chain_text: str,
        errors: List[str],
        warnings: List[str],
    ) -> set:
        """Tolerant validation of an optional **Chain** ladder (root -> ... -> D).

        Returns the node refs this Cause declares (chain rungs plus the always-valid
        ``root``/``D``) for cross-checking intervention targets. Absence is legal (a
        degenerate root->D chain). When present it must parse to ``<ref>:`` rungs — a
        Chain with none is a hard error (the pack builder would drop it); softer
        issues (missing root/D, overlong rung) warn."""
        valid_refs = {"root", "D"}
        if not chain_text.strip():
            return valid_refs
        rungs = CHAIN_RUNG_RE.findall(chain_text)
        if not rungs:
            errors.append(
                f"{label}: **Chain** is present but has no `<ref>:` rungs "
                "(use `root:`, `s1:`, ... `D:`); omit Chain for a degenerate chain"
            )
            return valid_refs
        refs = []
        for ref, stmt in rungs:
            if ref == CONVERGES_REF:
                continue  # convergence directive, not a node
            refs.append(ref)
            if len(stmt.strip()) > MAX_CHAIN_RUNG_LENGTH:
                warnings.append(
                    f"{label}: Chain rung '{ref}' is {len(stmt.strip())} chars "
                    f"(>{MAX_CHAIN_RUNG_LENGTH})"
                )
        valid_refs.update(refs)
        if "root" not in refs:
            warnings.append(f"{label}: **Chain** has no `root:` rung")
        if "D" not in refs:
            warnings.append(
                f"{label}: **Chain** has no `D:` terminal rung "
                "(points at Symptom Recognition)"
            )
        return valid_refs

    def _validate_interventions(
        self,
        label: str,
        interventions_text: str,
        valid_refs: set,
        errors: List[str],
        warnings: List[str],
    ) -> None:
        """Each intervention bullet must carry a valid quadrant tag and target a
        node the Cause declares. Missing/invalid quadrant is a hard error; an
        off-chain target ref, or a missing per-fix Verification/Risk/Duration, warns.
        Emptiness is already owned by the required-sub-field check (Gate 2a)."""
        if not interventions_text.strip():
            return
        marks = list(INTERVENTION_RE.finditer(interventions_text))
        if not marks:
            errors.append(
                f"{label}: **Interventions** has no quadrant-tagged entry "
                f"(`- **<quadrant>** (<ref>): ...`; quadrants: "
                f"{', '.join(sorted(INTERVENTION_QUADRANTS))})"
            )
            return
        for m in marks:
            quadrant, ref = m.group(1), m.group(2).strip()
            if quadrant not in INTERVENTION_QUADRANTS:
                errors.append(
                    f"{label}: intervention quadrant '{quadrant}' is not valid "
                    f"({', '.join(sorted(INTERVENTION_QUADRANTS))})"
                )
            if ref and ref != CONVERGES_REF and ref not in valid_refs:
                warnings.append(
                    f"{label}: intervention targets node '{ref}', which is not a "
                    "Chain rung or `root`/`D`"
                )
        # Per-intervention: each fix carries its own Verification; a `mitigation`
        # declares Risk + Duration. Slice the field at each bullet so one fix's
        # fields don't satisfy the check for another.
        for i, m in enumerate(marks):
            end = (
                marks[i + 1].start() if i + 1 < len(marks) else len(interventions_text)
            )
            block = interventions_text[m.start() : end]
            if "**Verification:**" not in block:
                warnings.append(
                    f"{label}: a `{m.group(1)}` intervention has no **Verification:**"
                )
            if m.group(1) == "mitigation":
                for needed in ("Risk", "Duration"):
                    if f"**{needed}:**" not in block:
                        warnings.append(
                            f"{label}: a `mitigation` intervention should declare "
                            f"**{needed}:**"
                        )
        if "```" not in interventions_text and self._looks_command_based(
            interventions_text
        ):
            warnings.append(
                f"{label}: **Interventions** has no fenced code block; fixes are "
                "usually command-based"
            )

    def _validate_indicator_field(
        self,
        label: str,
        indicator_text: str,
        diagnostic_steps: set,
        is_fallback: bool,
        errors: List[str],
        warnings: List[str],
    ) -> None:
        """Each Indicator entry must carry a ``[Step N]``/``[Symptom]``/``[Default]``
        token, and a ``[Step N]`` must resolve to a real ``## Diagnostic Steps``
        step. The fallback Cause's entries should be ``[Default]``. Emptiness is
        owned by the required-sub-field check (Gate 2a)."""
        entries = self._indicator_lines(indicator_text)
        if not entries:
            text = indicator_text.strip()
            if text:
                entries = [text]
        for entry in entries:
            if not INDICATOR_TOKEN_RE.findall(entry):
                errors.append(
                    f"{label}: Indicator entry has no [Step N] / [Symptom] / "
                    f"[Default] token: {entry!r}"
                )
                continue
            for step_ref in STEP_REF_RE.findall(entry):
                step_num = int(step_ref)
                if step_num not in diagnostic_steps:
                    errors.append(
                        f"{label}: Indicator references [Step {step_num}] which does "
                        "not exist in ## Diagnostic Steps"
                    )
        if is_fallback:
            for entry in entries:
                if FALLBACK_INDICATOR_TOKEN not in entry:
                    warnings.append(
                        f"{label}: fallback Cause Indicator entries should be "
                        f"{FALLBACK_INDICATOR_TOKEN}; got: {entry!r}"
                    )

    def _collect_step_numbers(self, content: str) -> set:
        """The set of ``### Step N:`` numbers declared in ## Diagnostic Steps."""
        body = _section_body(content, "Diagnostic Steps")
        if body is None:
            return set()
        return {int(m.group(1)) for m in STEP_HEADING_RE.finditer(body)}

    @staticmethod
    def _indicator_lines(indicator_text: str) -> List[str]:
        """Bullet entries of an Indicators field, leading bullet markers removed —
        the same lines the pack builder sees. Comments are already gone: the
        value came from a comment-masked Cause body (#1241)."""
        lines: List[str] = []
        for raw in indicator_text.splitlines():
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped[:2] in {"- ", "* ", "+ "}:
                stripped = stripped[2:].strip()
            elif (
                stripped[0] in {"-", "*", "+"}
                and len(stripped) > 1
                and stripped[1] == " "
            ):
                stripped = stripped[2:].strip()
            lines.append(stripped)
        return lines

    @staticmethod
    def _looks_command_based(body: str) -> bool:
        """Cheap heuristic to suppress the no-code-block warning when a fix is
        genuinely procedural ("escalate to vendor", "failover")."""
        non_command_phrases = (
            "escalate",
            "contact",
            "failover",
            "out of scope",
            "out of runbook scope",
            "n/a",
            "diagnostic only",
            "manual",
        )
        body_lower = body.lower()
        return not any(p in body_lower for p in non_command_phrases)

    def _validate_quality(self, content: str, warnings: List[str]) -> None:
        content_body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", content, flags=re.DOTALL)

        if len(content_body) < MIN_CONTENT_LENGTH:
            warnings.append(
                f"Content is short ({len(content_body)} < {MIN_CONTENT_LENGTH} characters)"
            )

        code_blocks = re.findall(r"```.*?```", content, re.DOTALL)
        if len(code_blocks) == 0:
            warnings.append("No code blocks found - consider adding examples")

        if not re.search(r"```(?:bash|shell|sh)", content):
            warnings.append("No shell command examples found")

        links = re.findall(r"\[([^\]]+)\]\(https?://[^\)]+\)", content)
        if len(links) == 0:
            warnings.append("No external references found")

    def _validate_security(
        self, content: str, errors: List[str], warnings: List[str]
    ) -> None:
        # Security hazards BLOCK (errors): a draft must not promote a leaked
        # credential or a destructive command into the KB. See module-level
        # find_security_hazards for the placeholder-aware secret + destructive
        # command detection.
        sec_errors, sec_warnings = find_security_hazards(content)
        errors.extend(sec_errors)
        warnings.extend(sec_warnings)


# =============================================================================
# The publication gate (#1214)
# =============================================================================
#
# The gate above used to be enforced only at the ``POST /knowledge/documents``
# ROUTE. Everything published to the KB becomes a
# ``KnowledgeItemType.RUNBOOK`` (``ingest_runbook`` hard-codes it) and
# ``upload_document`` writes a ``ConversionDraftModel`` with
# ``validation_passed=True`` — a claim the method itself never checked. The
# suggestion-approval path calls the service directly, so LLM-extracted
# markdown shaped ``## Problem / ## Root Cause / ## Solution / ## Prevention``
# would have been published as a runbook with the claim attached and none of
# the frontmatter the retrieval side filters on.
#
# So the gate moves DOWN to the service, where both callers meet, and the
# route keeps its own richer 422 (errors + warnings + authoring help). The
# route's copy is now a better message for the same decision rather than the
# only place the decision is made.


class RunbookQualityError(ValidationException):
    """Content was refused by the runbook quality gate.

    A ``ValidationException`` so the registered global handler answers **422** —
    the same status the upload route already answers by hand, reached from the
    service layer without the route having to translate anything.

    ``errors`` / ``warnings`` are kept as structured attributes for callers that
    want to render them; the flattened message carries them too, because
    ``validation_exception_handler`` renders ``str(exc)`` and a bare "does not
    meet quality standards" tells a reviewer nothing about what to fix.
    """

    def __init__(self, errors: List[str], warnings: Optional[List[str]] = None):
        self.errors = list(errors)
        self.warnings = list(warnings or [])
        joined = "; ".join(self.errors) if self.errors else "unspecified"
        super().__init__(
            f"Content does not meet runbook quality standards: {joined}",
            details={"errors": self.errors, "warnings": self.warnings},
        )


def enforce_runbook_quality(content: str) -> None:
    """Refuse content that would not pass the runbook quality gate.

    The single enforcement point for the two callers of
    :meth:`KnowledgeService.upload_document` — the ``POST /knowledge/documents``
    upload route and suggestion approval. It is deliberately NOT the gate for
    the whole knowledge base: ``ingest_runbook`` sits one level below and is the
    real convergence point, reached by three other paths that each bring their
    own guarantee — the KB pack bootstrap and the ``kb_seed`` maintenance job
    (validated at pack BUILD time in kb-toolkit, and made cheap by shipping
    pre-chunked vectors, so re-validating 91 runbooks on every boot would be
    paid for nothing) and conversion ``verify_draft`` (which validates the draft
    before promoting it). Moving the gate down would tax those three to cover
    two callers that are already covered here.

    Raises:
        RunbookQualityError: the content fails structural validation. Nothing is
            written — the gate runs before the first side effect.
    """
    result = RunbookValidator().validate_content(content)
    if not result.passed:
        raise RunbookQualityError(errors=result.errors, warnings=result.warnings)


# =============================================================================
# Quality Scorer
# =============================================================================


class QualityScorer:
    """Score runbook quality across 4 dimensions."""

    def __init__(self):
        self._validator = RunbookValidator()

    def score_file(self, file_path: Path) -> QualityScore:
        """Score a runbook file on disk."""
        content = file_path.read_text(encoding="utf-8")
        return self.score_content(content)

    def score_content(self, content: str) -> QualityScore:
        """Score runbook markdown content."""
        validation = self._validator.validate_content(content)

        completeness = self._score_completeness(content, validation)
        clarity = self._score_clarity(content)
        actionability = self._score_actionability(content)
        comprehensiveness = self._score_comprehensiveness(content)

        overall = (
            completeness * 0.30
            + clarity * 0.25
            + actionability * 0.25
            + comprehensiveness * 0.20
        )

        grade = self._calculate_grade(overall)

        return QualityScore(
            overall=round(overall, 1),
            grade=grade,
            completeness=round(completeness, 1),
            clarity=round(clarity, 1),
            actionability=round(actionability, 1),
            comprehensiveness=round(comprehensiveness, 1),
        )

    def _score_completeness(self, content: str, validation: ValidationResult) -> float:
        score = 100.0
        if not validation.passed:
            score -= len(validation.errors) * 10

        metadata = self._extract_metadata(content)
        required_meta = [
            "id",
            "title",
            "domain",
            "service",
            "symptom_class",
            "severity",
            "tags",
        ]
        missing = [m for m in required_meta if m not in metadata]
        score -= len(missing) * 5

        optional_meta = [
            "difficulty",
            "version",
            "last_updated",
            "verified_by",
            "scope",
        ]
        present_optional = [m for m in optional_meta if m in metadata]
        score += len(present_optional) * 2.5

        return max(0.0, min(100.0, score))

    def _score_clarity(self, content: str) -> float:
        score = 50.0

        headings = re.findall(r"^#{1,4}\s+.+$", content, re.MULTILINE)
        if len(headings) >= 5:
            score += 10
        elif len(headings) >= 3:
            score += 5

        code_blocks = re.findall(r"```\w+", content)
        if len(code_blocks) >= 3:
            score += 10
        elif len(code_blocks) >= 1:
            score += 5

        lists = re.findall(r"^[-*]\s+.+$|^\d+\.\s+.+$", content, re.MULTILINE)
        if len(lists) >= 10:
            score += 10
        elif len(lists) >= 5:
            score += 5

        steps = re.findall(r"(?:Step \d+|^\d+\.)", content, re.MULTILINE)
        if len(steps) >= 5:
            score += 10
        elif len(steps) >= 3:
            score += 5

        paragraphs = content.split("\n\n")
        long_paragraphs = [p for p in paragraphs if len(p) > 500]
        score -= len(long_paragraphs) * 2

        return max(0.0, min(100.0, score))

    def _score_actionability(self, content: str) -> float:
        score = 40.0

        shell_commands = re.findall(r"```(?:bash|sh)\n(.+?)```", content, re.DOTALL)
        if len(shell_commands) >= 5:
            score += 15
        elif len(shell_commands) >= 3:
            score += 10
        elif len(shell_commands) >= 1:
            score += 5

        tools = re.findall(r"(?:kubectl|docker|systemctl|journalctl|grep|awk)", content)
        if len(tools) >= 5:
            score += 10
        elif len(tools) >= 3:
            score += 5

        diagnostic_keywords = re.findall(
            r"(?i)(check|verify|examine|inspect|test|debug)", content
        )
        if len(diagnostic_keywords) >= 10:
            score += 10
        elif len(diagnostic_keywords) >= 5:
            score += 5

        # v4: fixes are quadrant-tagged Interventions per Cause, not top-level sections
        causes_section = re.search(r"(?i)^#{1,4}\s*Causes", content, re.MULTILINE)
        if causes_section:
            score += 10

        has_fix = re.search(
            r"(?im)^\s*\*\*Interventions:\*\*"
            rf"|^\s*-\s*\*\*({QUADRANT_ALTERNATION})\*\*",
            content,
        )
        if has_fix:
            score += 5

        command_explanations = re.findall(r"```.*?```\s*\n\s*[A-Z]", content, re.DOTALL)
        if len(command_explanations) >= 3:
            score += 10

        return max(0.0, min(100.0, score))

    def _score_comprehensiveness(self, content: str) -> float:
        score = 30.0

        word_count = len(content.split())
        if word_count >= 1500:
            score += 20
        elif word_count >= 1000:
            score += 15
        elif word_count >= 500:
            score += 10
        elif word_count >= 300:
            score += 5

        root_cause = re.findall(
            r"(?i)(?:root cause|why this happens|underlying issue|permanent fix"
            r"|\*\*Statement:\*\*|\*\*Chain:\*\*)",
            content,
        )
        if len(root_cause) >= 1:
            score += 15

        prevention = re.findall(
            r"(?i)(?:prevention|avoid|best practice|recommendation)", content
        )
        if len(prevention) >= 3:
            score += 10
        elif len(prevention) >= 1:
            score += 5

        has_sources = bool(re.search(r"(?i)^#{1,4}\s*Sources", content, re.MULTILINE))
        if has_sources:
            score += 10

        # v3: Verification is a per-Cause sub-field
        verification = re.search(
            r"(?i)(?:\*\*Verification:\*\*|how to confirm)",
            content,
        )
        if verification:
            score += 10

        return max(0.0, min(100.0, score))

    def _calculate_grade(self, score: float) -> str:
        if score >= 90:
            return "A"
        elif score >= 80:
            return "B"
        elif score >= 70:
            return "C"
        elif score >= 60:
            return "D"
        else:
            return "F"

    def _extract_metadata(self, content: str) -> Dict:
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if match:
            try:
                return yaml.safe_load(match.group(1)) or {}
            except Exception:
                return {}
        return {}
