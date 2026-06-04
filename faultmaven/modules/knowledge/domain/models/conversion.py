"""Domain models for document-to-runbook conversion.

Defines the data structures for the conversion pipeline:
- ConversionJob: Tracks a conversion request lifecycle
- ConversionDraft: Individual runbook draft generated from a source document
- Analysis models: LLM analysis and failure mode detection results
- Request/Response models: API contract types
"""

import hashlib
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

# =============================================================================
# Enums
# =============================================================================


class ConversionStatus(str, Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class DraftStatus(str, Enum):
    DRAFT = "draft"
    VERIFIED = "verified"
    DISCARDED = "discarded"


class SourceType(str, Enum):
    DOCUMENT = "document"
    CASE = "case"


class ConversionErrorCode(str, Enum):
    """Structured error codes for frontend translation."""

    # File-level errors
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    FILE_CORRUPT = "FILE_CORRUPT"
    FILE_EMPTY = "FILE_EMPTY"
    ENCODING_ERROR = "ENCODING_ERROR"

    # Content-level errors
    DOCUMENT_TOO_LONG = "DOCUMENT_TOO_LONG"
    DOCUMENT_TOO_SHORT = "DOCUMENT_TOO_SHORT"
    NOT_ACTIONABLE = "NOT_ACTIONABLE"
    NO_FAILURE_MODES = "NO_FAILURE_MODES"
    ALREADY_A_RUNBOOK = "ALREADY_A_RUNBOOK"
    NO_TECHNICAL_CONTENT = "NO_TECHNICAL_CONTENT"

    # LLM errors
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    LLM_PARSE_ERROR = "LLM_PARSE_ERROR"
    LLM_GENERATION_FAILED = "LLM_GENERATION_FAILED"

    # Auth errors
    INSUFFICIENT_PERMISSIONS = "INSUFFICIENT_PERMISSIONS"

    # Validation errors
    VALIDATION_FAILED = "VALIDATION_FAILED"
    DRAFT_NOT_FOUND = "DRAFT_NOT_FOUND"
    DRAFT_ALREADY_VERIFIED = "DRAFT_ALREADY_VERIFIED"


# =============================================================================
# LLM Analysis Models (structured output from analysis call)
# =============================================================================


class FailureModeAnalysis(BaseModel):
    id: str
    title: str
    domain: str
    service: str
    symptom_class: List[str]
    severity: str
    symptoms_summary: str
    resolution_summary: str


class SourceAssessment(BaseModel):
    content_type: str
    actionability_rating: str = Field(description="low, medium, or high")
    missing_information: List[str]


class AnalysisResult(BaseModel):
    is_actionable: bool
    failure_modes: List[FailureModeAnalysis]
    source_assessment: SourceAssessment


# =============================================================================
# Content Triage Model (structured output from classifier call)
# =============================================================================


class TriageResult(BaseModel):
    is_actionable: bool
    confidence: float
    reason: str


# =============================================================================
# Preprocessing Result
# =============================================================================


class RedactionEntry(BaseModel):
    type: str
    count: int
    context: str


class RedactionReport(BaseModel):
    redactions: List[RedactionEntry] = Field(default_factory=list)
    warning: Optional[str] = None
    total_redacted: int = 0


class PreprocessingResult(BaseModel):
    extracted_text: str
    source_metadata: dict
    redaction_report: RedactionReport = Field(default_factory=RedactionReport)
    triage_result: Optional[TriageResult] = None
    warnings: List[str] = Field(default_factory=list)
    is_rejected: bool = False
    rejection_reason: Optional[str] = None
    error_code: Optional[str] = None
    token_count: int = 0
    is_existing_runbook: bool = False


# =============================================================================
# Validation and Quality Models
# =============================================================================


class ValidationResult(BaseModel):
    passed: bool
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class QualityScore(BaseModel):
    overall: float
    grade: str
    completeness: float
    clarity: float
    actionability: float
    comprehensiveness: float


# =============================================================================
# Source File Info
# =============================================================================


class SourceFileInfo(BaseModel):
    filename: str
    size_bytes: int
    content_type: str
    retained_path: Optional[str] = None


# =============================================================================
# Conversion Draft
# =============================================================================


class ConversionDraft(BaseModel):
    draft_id: str
    runbook_id: str
    title: str
    scope: str
    status: DraftStatus = DraftStatus.DRAFT
    source_type: SourceType = SourceType.DOCUMENT
    case_id: Optional[str] = None
    validation: ValidationResult
    quality_score: QualityScore
    file_path: str
    content_preview: str = Field(
        max_length=500, description="First 500 chars of generated markdown"
    )
    content: Optional[str] = Field(
        default=None, description="Full markdown content, included on detail requests"
    )
    quality_warning: Optional[str] = Field(
        default=None, description="Warning if quality score < 50"
    )


# =============================================================================
# Conversion Error (for partial failures)
# =============================================================================


class ConversionError(BaseModel):
    failure_mode_id: str
    error: str
    retryable: bool = False


# =============================================================================
# Conversion Response
# =============================================================================


class ConversionResponse(BaseModel):
    conversion_id: str
    status: ConversionStatus
    source_file: SourceFileInfo
    analysis: AnalysisResult
    drafts: List[ConversionDraft]
    warnings: List[str] = Field(default_factory=list)
    created_at: datetime


# =============================================================================
# Draft Management Request/Response
# =============================================================================


class DraftUpdateRequest(BaseModel):
    content: str = Field(
        min_length=100,
        description="Full runbook markdown content including frontmatter",
    )


class VerifyResponse(BaseModel):
    draft_id: str
    runbook_id: str
    status: str = "verified"
    knowledge_item_id: str
    ingested: bool
    ingested_at: Optional[datetime] = None
    collection: str
    chunks_created: int


# =============================================================================
# Case-to-Runbook Conversion Request
# =============================================================================


class CaseConversionRequest(BaseModel):
    """Data extracted from a RESOLVED case for runbook generation.

    Runbooks codify complete troubleshooting scenarios — root cause + verified
    solution — so only RESOLVED cases reach this request shape.

    Fields are populated from the Case domain model:
    - title: Case.title
    - description: Case.problem_verification.symptom_statement
    - root_cause: Case.root_cause_conclusion.root_cause
    - root_cause_mechanism: Case.root_cause_conclusion.mechanism
    - solutions: Structured text from Case.solutions[] (title, steps, commands, risks)
    - hypotheses_summary: Validated hypothesis statements from Case.hypotheses
    - evidence_summary: Case.working_conclusion.statement + evidence summaries
    - severity: Case.problem_verification.severity
    - service: Case.problem_verification.affected_services[0]
    """

    case_id: str
    title: str
    description: str
    root_cause: Optional[str] = None
    root_cause_mechanism: Optional[str] = None
    solutions: List[str] = Field(default_factory=list)
    hypotheses_summary: str = ""
    evidence_summary: str = ""
    domain: str = "application"
    service: str = "unknown"
    symptom_class: List[str] = Field(default_factory=list)
    severity: str = "medium"
    tags: List[str] = Field(default_factory=list)
    scope: str = "personal"

    @classmethod
    def from_case(cls, case, scope: str = "personal") -> "CaseConversionRequest":
        """Extract runbook generation data from a RESOLVED Case domain object.

        Reusable by both the API route and the milestone engine.
        """
        # Root cause
        rc_obj = getattr(case, "root_cause_conclusion", None)
        root_cause = getattr(rc_obj, "root_cause", None) if rc_obj else None
        rc_mechanism = getattr(rc_obj, "mechanism", None) if rc_obj else None

        # Problem description
        pv = getattr(case, "problem_verification", None)
        symptom = (getattr(pv, "symptom_statement", "") or "") if pv else ""
        severity = (getattr(pv, "severity", "medium") or "medium") if pv else "medium"
        affected = (getattr(pv, "affected_services", []) or []) if pv else []

        # Solutions
        solutions = []
        for sol in getattr(case, "solutions", []) or []:
            parts = []
            if t := getattr(sol, "title", None):
                parts.append(f"Solution: {t}")
            if v := getattr(sol, "immediate_action", None):
                parts.append(f"Immediate action: {v}")
            if v := getattr(sol, "longterm_fix", None):
                parts.append(f"Permanent fix: {v}")
            for attr, label, fmt in [
                (
                    "implementation_steps",
                    "Steps",
                    lambda s: "\n".join(f"  {i+1}. {x}" for i, x in enumerate(s)),
                ),
                ("commands", "Commands", lambda s: "\n".join(f"  $ {x}" for x in s)),
                ("risks", "Risks", lambda s: "; ".join(s)),
            ]:
                if v := getattr(sol, attr, None):
                    parts.append(
                        f"{label}:\n{fmt(v)}"
                        if attr != "risks"
                        else f"{label}: {fmt(v)}"
                    )
            if parts:
                solutions.append("\n".join(parts))

        # Hypotheses
        hypotheses = getattr(case, "hypotheses", {}) or {}
        validated = []
        for h_id, h in hypotheses.items() if isinstance(hypotheses, dict) else []:
            status = getattr(h, "state", None)
            val = status.value if hasattr(status, "value") else str(status)
            if val == "validated":
                validated.append(getattr(h, "statement", None) or str(h))
        hyp_summary = "; ".join(validated) if validated else ""

        # Evidence summary
        wc = getattr(case, "working_conclusion", None)
        ev_summary = ""
        if wc:
            ev_summary = getattr(wc, "statement", "") or ""
            if r := getattr(wc, "reasoning", None):
                ev_summary += f"\nReasoning: {r}"
        briefs = [
            getattr(e, "summary", None) for e in (getattr(case, "evidence", []) or [])
        ]
        briefs = [b for b in briefs if b][:10]
        if briefs:
            ev_summary += "\n\nKey evidence:\n" + "\n".join(f"- {b}" for b in briefs)

        service = affected[0] if affected else "unknown"
        tags = getattr(case, "tags", []) or []

        # Resolve `domain` to one of the 7 taxonomy values so the generated
        # runbook passes validation. The keyword map is intentionally narrow
        # — match on service names, tags, and root-cause/symptom text. When
        # nothing matches we fall back to "application" (the broadest valid
        # bucket) rather than "general" (rejected by RunbookValidator).
        signal_text = " ".join(
            [
                service,
                " ".join(tags),
                " ".join(affected),
                symptom,
                root_cause or "",
                rc_mechanism or "",
            ]
        ).lower()
        domain = _resolve_domain(signal_text)

        return cls(
            case_id=case.case_id,
            title=getattr(case, "title", "Untitled Case") or "Untitled Case",
            description=symptom,
            root_cause=root_cause,
            root_cause_mechanism=rc_mechanism,
            solutions=solutions,
            hypotheses_summary=hyp_summary,
            evidence_summary=ev_summary,
            domain=domain,
            service=service,
            severity=severity.lower() if isinstance(severity, str) else "medium",
            tags=tags if tags else affected,
            scope=scope,
        )


# =============================================================================
# Domain Resolution
# =============================================================================

# Keyword maps for case-to-domain classification. Must stay aligned with the
# 7 valid domain values in RunbookValidator (database, networking, compute,
# application, security, storage, messaging). "application" is the catch-all
# default and intentionally has no keywords — anything that matches none of
# the others lands there.
_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "networking": (
        "istio",
        "envoy",
        "service mesh",
        "loadbalancer",
        "load balancer",
        "ingress",
        "egress",
        "dns",
        "tls",
        "ssl",
        "certificate",
        "proxy",
        "nginx",
        "haproxy",
        "destinationrule",
        "virtualservice",
        "gateway",
        "route",
        "routing",
        "upstream",
        "downstream",
        "connection pool",
        "503",
        "504",
        "502",
        "network",
        "tcp",
        "udp",
        "grpc",
    ),
    "database": (
        "postgres",
        "postgresql",
        "mysql",
        "mariadb",
        "redis",
        "memcached",
        "mongodb",
        "mongo",
        "cassandra",
        "elasticsearch",
        "opensearch",
        "rds",
        "deadlock",
        "vacuum",
        "replication lag",
        "replica lag",
        "query",
        "index",
        "sql",
        "db pool",
        "connection pool exhaustion",
    ),
    "compute": (
        "kubernetes",
        "k8s",
        "pod",
        "container",
        "docker",
        "deployment",
        "statefulset",
        "replicaset",
        "daemonset",
        "node",
        "oom",
        "out of memory",
        "cpu throttl",
        "memory leak",
        "crashloop",
        "imagepull",
        "ec2",
        "vm",
    ),
    "security": (
        "auth",
        "oauth",
        "jwt",
        "rbac",
        "iam",
        "permission",
        "forbidden",
        "unauthorized",
        " 401",
        " 403",
        "secret",
        "vault",
        "credential",
        "token",
        "cve",
        "vulnerability",
        "encryption",
    ),
    "storage": (
        "s3",
        "ebs",
        "efs",
        "pvc",
        " pv ",
        "volume",
        "disk full",
        "no space",
        "blob storage",
        "gcs",
        "azure storage",
        "bucket",
        "object storage",
    ),
    "messaging": (
        "kafka",
        "rabbitmq",
        "pubsub",
        "sqs",
        "sns",
        "kinesis",
        "queue",
        "topic",
        "partition",
        "consumer lag",
        "broker",
        "nats",
        "activemq",
    ),
}


def _resolve_domain(text: str) -> str:
    """Pick the runbook taxonomy domain that best matches the case signals.

    Counts substring matches per domain over ``text`` (already lowercased by
    the caller) and returns the highest-scoring domain. Ties are broken by
    the dict iteration order. Falls back to "application" when no keyword
    fires — the catch-all bucket for web apps, microservices, and generic
    app-layer issues that the other domains don't cover.
    """
    if not text:
        return "application"
    scores: dict[str, int] = {}
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        scores[domain] = sum(1 for k in keywords if k in text)
    best_domain, best_score = max(scores.items(), key=lambda kv: kv[1])
    if best_score == 0:
        return "application"
    return best_domain


# =============================================================================
# ID Generation Utilities
# =============================================================================


def generate_conversion_id() -> str:
    return f"conv_{uuid.uuid4().hex[:12]}"


def generate_draft_id() -> str:
    return f"draft_{uuid.uuid4().hex[:12]}"


def generate_runbook_id(failure_mode: FailureModeAnalysis) -> str:
    """Generate kebab-case ID from service and failure description."""
    base = f"{failure_mode.service}-{failure_mode.title}"
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    if len(slug) > 60:
        slug = slug[:55] + "-" + hashlib.md5(slug.encode()).hexdigest()[:4]
    return slug
