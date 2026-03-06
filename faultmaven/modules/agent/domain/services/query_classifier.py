"""Query classifier for scenario-driven data processing.

Classifies user messages as either Triage (generic request) or Directed
Analysis (specific inquiry) based on entity detection heuristics. No LLM
call — this is a fast, deterministic classifier.

Design Reference: docs/architecture/data-processing/README.md
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ProcessingMode(str, Enum):
    """How evidence should be processed based on the user's query."""

    TRIAGE = "triage"
    DIRECTED_ANALYSIS = "directed_analysis"
    SEMANTIC_SEARCH = "semantic_search"


@dataclass
class QueryClassification:
    """Result of classifying a user message for processing mode routing."""

    mode: ProcessingMode
    detected_entities: dict[str, list[str]] = field(default_factory=dict)
    confidence: float = 0.0


# --- Entity extraction patterns ---

# Timestamps: HH:MM, HH:MM:SS, ISO dates, Unix-style dates
_TIMESTAMP_PATTERNS = [
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"),  # HH:MM or HH:MM:SS
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),  # YYYY-MM-DD
    re.compile(
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\b", re.I
    ),  # Mon DD
]

# HTTP status codes: 4xx, 5xx
_STATUS_CODE_PATTERN = re.compile(r"\b[45]\d{2}\b")

# Error keywords that indicate specific technical conditions
_ERROR_KEYWORDS = frozenset(
    {
        "oom",
        "segfault",
        "sigsegv",
        "sigkill",
        "sigterm",
        "killed",
        "connection refused",
        "connection timeout",
        "connection reset",
        "deadlock",
        "out of memory",
        "heap",
        "stack overflow",
        "null pointer",
        "nullpointerexception",
        "core dump",
        "kernel panic",
        "disk full",
        "no space left",
        "permission denied",
        "authentication failed",
        "certificate expired",
        "dns resolution",
        "socket timeout",
        "broken pipe",
        "connection pool",
        "thread pool",
        "gc pause",
        "full gc",
        "slow query",
        "lock wait",
        "replication lag",
    }
)

# Generic phrases that signal triage mode (no specific inquiry)
_GENERIC_PHRASES = [
    re.compile(r"\b(?:analyze|check|look at|review|examine)\s+(?:this|the|my)\b", re.I),
    re.compile(r"\bwhat(?:'s| is) (?:in (?:here|this|the)|going on)\b", re.I),
    re.compile(r"\banything (?:wrong|unusual|interesting|notable)\b", re.I),
    re.compile(r"\bsee any (?:issues?|problems?|errors?|trouble)\b", re.I),
    re.compile(r"\bcan you (?:look|check|take a look)\b", re.I),
    re.compile(r"\bhere(?:'s| is) (?:the|my|a)\b", re.I),
    re.compile(r"\btell me what(?:'s| is)\b", re.I),
    re.compile(r"\bwhat do you (?:see|think|find)\b", re.I),
]

# Service/host name pattern: common infra names
_SERVICE_PATTERN = re.compile(
    r"\b(?:nginx|apache|postgres(?:ql)?|mysql|redis|kafka|rabbit(?:mq)?|"
    r"elasticsearch|kibana|grafana|prometheus|docker|kubernetes|k8s|"
    r"etcd|consul|vault|haproxy|envoy|istio|mongo(?:db)?|"
    r"memcache[d]?|zookeeper|cassandra|dynamo(?:db)?|"
    r"node|java|python|go|rust|tomcat|jetty|gunicorn|uvicorn|"
    r"systemd|journald|syslog|cron)\b",
    re.I,
)

# IP addresses and ports
_IP_PORT_PATTERN = re.compile(r"\b(?:\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?)\b")


def _extract_entities(message: str) -> dict[str, list[str]]:
    """Extract technical entities from a user message."""
    entities: dict[str, list[str]] = {}

    # Timestamps
    timestamps = []
    for pattern in _TIMESTAMP_PATTERNS:
        timestamps.extend(pattern.findall(message))
    if timestamps:
        entities["timestamps"] = timestamps

    # HTTP status codes
    status_codes = _STATUS_CODE_PATTERN.findall(message)
    if status_codes:
        entities["status_codes"] = status_codes

    # Error keywords
    message_lower = message.lower()
    found_keywords = [kw for kw in _ERROR_KEYWORDS if kw in message_lower]
    if found_keywords:
        entities["error_keywords"] = found_keywords

    # Service names
    services = _SERVICE_PATTERN.findall(message)
    if services:
        entities["services"] = list(set(s.lower() for s in services))

    # IP addresses
    ips = _IP_PORT_PATTERN.findall(message)
    if ips:
        entities["ip_addresses"] = ips

    return entities


def _is_generic_request(message: str) -> bool:
    """Check if the message matches generic/triage-like phrasing."""
    return any(pattern.search(message) for pattern in _GENERIC_PHRASES)


def _has_interrogative_structure(message: str) -> bool:
    """Check if the message contains a specific question structure."""
    # Question mark with content beyond just generic phrasing
    if "?" not in message:
        return False
    # Check for interrogative words followed by specific content
    interrogative = re.search(
        r"\b(?:what|why|when|where|how|which|who|did|does|is|are|was|were|has|have|can|could)\b",
        message,
        re.I,
    )
    return interrogative is not None


def classify_query(
    user_message: str, has_attachments: bool = False
) -> QueryClassification:
    """Classify a user message for processing mode routing.

    Routes to:
    - TRIAGE: generic requests, no specific inquiry, file-only uploads
    - DIRECTED_ANALYSIS: specific questions with entities, timestamps,
      error codes, or technical conditions to investigate

    Ambiguous cases default to DIRECTED_ANALYSIS since DA subsumes
    Triage via cold-start orientation (structural index is always available).

    Args:
        user_message: The user's message text
        has_attachments: Whether the message includes file attachments

    Returns:
        QueryClassification with mode, detected entities, and confidence
    """
    message = (user_message or "").strip()

    # No message + attachments → Triage (user just dropped a file)
    if not message and has_attachments:
        return QueryClassification(
            mode=ProcessingMode.TRIAGE,
            confidence=0.95,
        )

    # No message, no attachments → Triage (nothing to direct)
    if not message:
        return QueryClassification(
            mode=ProcessingMode.TRIAGE,
            confidence=0.5,
        )

    entities = _extract_entities(message)
    is_generic = _is_generic_request(message)
    has_question = _has_interrogative_structure(message)
    has_entities = bool(entities)

    # Specific entities + question → Directed Analysis (high confidence)
    if has_entities and has_question:
        return QueryClassification(
            mode=ProcessingMode.DIRECTED_ANALYSIS,
            detected_entities=entities,
            confidence=0.9,
        )

    # Specific entities without question mark → still DA (e.g., "check the 502s at 14:00")
    if has_entities and not is_generic:
        return QueryClassification(
            mode=ProcessingMode.DIRECTED_ANALYSIS,
            detected_entities=entities,
            confidence=0.75,
        )

    # Generic phrasing without entities → Triage
    if is_generic and not has_entities:
        return QueryClassification(
            mode=ProcessingMode.TRIAGE,
            confidence=0.85,
        )

    # Question mark but no entities and not generic → could be a specific
    # question using non-technical language. Default to DA since it subsumes Triage.
    if has_question and not is_generic:
        return QueryClassification(
            mode=ProcessingMode.DIRECTED_ANALYSIS,
            confidence=0.6,
        )

    # Generic with entities → entities win, route to DA
    if is_generic and has_entities:
        return QueryClassification(
            mode=ProcessingMode.DIRECTED_ANALYSIS,
            detected_entities=entities,
            confidence=0.65,
        )

    # Ambiguous: default to DA (it subsumes Triage via cold-start orientation)
    return QueryClassification(
        mode=ProcessingMode.DIRECTED_ANALYSIS,
        detected_entities=entities,
        confidence=0.5,
    )
