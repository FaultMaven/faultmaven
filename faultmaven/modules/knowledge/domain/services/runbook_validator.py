"""Inline runbook validation and quality scoring (v4 causal-chain schema).

Replicated from faultmaven-kb-toolkit to avoid cross-repo dependency.
Rules are aligned with the KB Toolkit's RunbookValidator and QualityScorer:
each `### Cause` declares one ROOT with sub-fields **Statement** / optional
**Chain** / **Indicators** / quadrant-tagged **Interventions**.
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from faultmaven.modules.knowledge.domain.models.conversion import (
    QualityScore,
    ValidationResult,
)
from faultmaven.modules.knowledge.domain.services.cause_grammar import (
    FALLBACK_CAUSE_LETTER,
    FALLBACK_INDICATOR_TOKEN,
    LEGACY_V3_CAUSE_SUBFIELDS,
    QUADRANT_ALTERNATION,
    REQUIRED_CAUSE_SUBFIELDS,
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

MAX_TITLE_LENGTH = 100
MIN_CONTENT_LENGTH = 500
MAX_TAG_COUNT = 10
# Hard limit on a Cause **Statement** (the match surface). Mirrors the kb-toolkit
# generator/validator (``config.validation.cause_statement_max_chars``) and the
# Template Compliance Rules (runbook-content-architecture.md §3, rule 5). Kept a
# local literal here — it is a scalar limit, NOT part of the cross-repo authoring
# VOCABULARY guarded by ``cause_grammar``/``check_vocab_cross_repo.py``.
MAX_CAUSE_STATEMENT_LENGTH = 300


# =============================================================================
# Cause-block parsing (shared by the per-Cause structural + Statement checks)
# =============================================================================

_CAUSE_HEADING_RE = re.compile(r"^#{3,}\s+Cause\s+([A-Za-z0-9]+)\s*:", re.MULTILINE)


def _cause_field(name: str, body: str) -> str:
    """Value of a ``**Name:**`` sub-field within one Cause block ("" if absent OR
    present-but-empty). The value runs up to the next bold ``**Label:**`` (the
    colon is required, so an inline ``**bold**`` doesn't terminate it), the next
    ``##``+ heading, or end. ``[ \\t]*`` (not ``\\s*``) so an empty field captures
    "" rather than swallowing the field that follows it."""
    m = re.search(
        rf"\*\*{re.escape(name)}:\*\*[ \t]*(.*?)(?=\n\s*\*\*[\w ]+:\*\*|\n#{{2,}}\s|\Z)",
        body,
        re.DOTALL,
    )
    return m.group(1).strip() if m else ""


def _has_cause_field(name: str, body: str) -> bool:
    """True if the ``**Name:**`` label is present — distinguishes a MISSING
    sub-field from a present-but-empty one (which ``_cause_field`` both return "")."""
    return bool(re.search(rf"\*\*{re.escape(name)}:\*\*", body))


def _iter_cause_blocks(content: str):
    """Yield ``(letter, body)`` for each ``### Cause X:`` block (coarse,
    document-level split — this validator does not build the full cause graph)."""
    heads = list(_CAUSE_HEADING_RE.finditer(content))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(content)
        yield h.group(1), content[h.end() : end]


def _is_fallback_cause(letter: str, body: str) -> bool:
    """The fallback Cause carries ``[Default]`` in its Indicators or the reserved
    letter Z — the same scoping the matcher's fallback detection uses."""
    return (
        FALLBACK_INDICATOR_TOKEN in _cause_field("Indicators", body)
        or letter.upper() == FALLBACK_CAUSE_LETTER
    )


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

        # Symptom class
        if "symptom_class" in metadata:
            sc = metadata["symptom_class"]
            if isinstance(sc, list):
                for item in sc:
                    if isinstance(item, str) and not re.match(r"^[a-z0-9_-]+$", item):
                        errors.append(
                            f"symptom_class must be lowercase with hyphens/underscores: {item}"
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
            pattern = rf"^##+ {re.escape(section)}"
            if not re.search(pattern, content, re.MULTILINE):
                errors.append(f"Missing required section: {section}")

        # ## Causes must have at least one ### Cause subsection
        if re.search(r"^##+ Causes", content, re.MULTILINE):
            cause_subsections = re.findall(r"^###+ Cause\s+\w", content, re.MULTILINE)
            if not cause_subsections:
                errors.append(
                    "## Causes section must contain at least one ### Cause subsection"
                )
            # Fallback cause (Cause Z with [Default] indicator) is a quality warning
            if FALLBACK_INDICATOR_TOKEN not in content:
                warnings.append(
                    f"No fallback Cause with {FALLBACK_INDICATOR_TOKEN} indicator "
                    f"found — add a '### Cause {FALLBACK_CAUSE_LETTER}: Unidentified' "
                    f"with {FALLBACK_INDICATOR_TOKEN} indicator"
                )
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
        for letter, body in _iter_cause_blocks(content):
            label = f"Cause {letter}"
            for sub in REQUIRED_CAUSE_SUBFIELDS:
                if not _has_cause_field(sub, body):
                    errors.append(f"{label}: missing required **{sub}:** sub-field")
                elif not _cause_field(sub, body):
                    errors.append(f"{label}: **{sub}:** sub-field is empty")
            stmt = _cause_field("Statement", body)
            if stmt and len(stmt) > MAX_CAUSE_STATEMENT_LENGTH:
                errors.append(
                    f"{label}: Statement is {len(stmt)} chars "
                    f"(>{MAX_CAUSE_STATEMENT_LENGTH})"
                )

    def _validate_cause_statements(
        self, content: str, errors: List[str], warnings: List[str]
    ) -> None:
        """Match-surface invariants (#545) on NON-FALLBACK Cause Statements.

        Drop the fallback Cause (``[Default]`` in its *Indicators*, or letter Z —
        it is not a match surface), collect each remaining Cause's non-empty
        ``**Statement:**``, and run the shared ``check_cause_statement_invariants``
        (no ``[Step N]`` leak; siblings mutually discriminative). Missing/empty
        Statements are owned by ``_validate_cause_subfields`` (Gate 2a); an empty
        one is simply skipped here (a non-empty Statement is needed for the check).
        """
        statements: List[tuple] = []
        for letter, body in _iter_cause_blocks(content):
            if _is_fallback_cause(letter, body):
                continue  # fallback Cause — not a match surface
            stmt = _cause_field("Statement", body)
            if stmt:
                statements.append((letter, stmt))
        errs, warns = check_cause_statement_invariants(statements)
        errors.extend(errs)
        warnings.extend(warns)

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
