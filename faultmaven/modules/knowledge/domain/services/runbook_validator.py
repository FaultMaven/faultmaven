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
            if not re.search(r"\[Default\]", content):
                warnings.append(
                    "No fallback Cause with [Default] indicator found — "
                    "add a '### Cause Z: Unidentified' with [Default] indicator"
                )
            # v4: each ### Cause carries Statement / Indicators / Interventions
            # (Chain optional); interventions are quadrant-tagged. Coarse,
            # document-level checks (drafts get human review before activation).
            for sub in ("Statement", "Indicators", "Interventions"):
                if not re.search(rf"\*\*{sub}:\*\*", content):
                    warnings.append(
                        f"No `**{sub}:**` sub-field found anywhere in the runbook "
                        "(v4 Causes use Statement / Indicators / Interventions)"
                    )
            if re.search(r"\*\*Interventions:\*\*", content) and not re.search(
                r"\*\*(remediation|defensive_fix|mitigation|loop_break)\*\*", content
            ):
                warnings.append(
                    "Interventions present but no quadrant tag "
                    "(remediation / defensive_fix / mitigation / loop_break)"
                )
            # v4 has no AND-sets in authored runbooks; flag legacy v3 sub-fields.
            if re.search(r"\*\*(Mechanism|Mitigation|Resolution):\*\*", content):
                warnings.append(
                    "Found v3 Cause sub-field(s) (Mechanism/Mitigation/Resolution) — "
                    "v4 uses Statement / Chain / Indicators / quadrant-tagged Interventions"
                )

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
        # credential or a destructive command into the KB. A secret match whose
        # value is an obvious placeholder / shell-or-template var stays non-blocking.
        placeholder_re = re.compile(
            r"[<>${}]|(?i:your[-_]|change[-_]?me|example|placeholder|redacted|x{3,}|todo|\.\.\.)"
        )
        secret_patterns = [
            (r"password\s*=\s*['\"]([^'\"]+)['\"]", "Potential hardcoded password"),
            (r"api[_-]?key\s*=\s*['\"]([^'\"]+)['\"]", "Potential hardcoded API key"),
            (r"secret\s*=\s*['\"]([^'\"]+)['\"]", "Potential hardcoded secret"),
            (r"token\s*=\s*['\"]([^'\"]+)['\"]", "Potential hardcoded token"),
        ]
        for pattern, message in secret_patterns:
            for m in re.finditer(pattern, content, re.IGNORECASE):
                if placeholder_re.search(m.group(1)):
                    warnings.append(f"Security: {message} (placeholder — not blocking)")
                else:
                    errors.append(f"Security: {message} (hardcoded credential)")

        dangerous = [
            (
                r"rm\s+-rf\s+(?:--no-preserve-root\s+)?/+(?:[\w.-]+/?)?(?:\s|\*|$)",
                "Dangerous command: rm -rf / (root or a top-level system directory)",
            ),
            (r":\(\)\{.*:\|:.*\};:", "Dangerous: fork bomb"),
            (r"dd\s+if=/dev/zero", "Potentially destructive: dd command"),
        ]
        for pattern, message in dangerous:
            if re.search(pattern, content):
                errors.append(f"Security: {message}")


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
            r"|^\s*-\s*\*\*(remediation|defensive_fix|mitigation|loop_break)\*\*",
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
