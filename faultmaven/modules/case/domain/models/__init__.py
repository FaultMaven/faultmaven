"""Case data models - Milestone-based investigation system.

This module defines the complete data structure for FaultMaven's investigation system
based on the Investigation Architecture Specification v2.0.

Key Models:
- Case: Root case entity with milestone-based progress tracking
- CaseState: Lifecycle state (INQUIRY -> INVESTIGATING -> RESOLVED/CLOSED)
- InvestigationProgress: 7 milestones tracking verification, diagnosis, and resolution
- ProblemVerification: Consolidated symptom, scope, timeline, and changes data
- Evidence: Categorized evidence collection with hypothesis evaluation
- Hypothesis: Optional systematic root cause exploration
- Solution: Proposed and applied solutions with verification

Architecture:
- Milestone-based progress (not phase-based)
- Two-track lifecycle: Status (user-facing) + Progress (internal detail)
- Evidence-driven advancement
- Optional hypotheses for systematic exploration
- Repository abstraction (no direct database imports)
"""

import logging
import re
from bisect import bisect_right
from datetime import UTC, datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

from .case import _MAX_TURN_BACKFILL, Case
from .causal import (
    CausalEdge,
    CausalNode,
    InterventionQuadrant,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ValidationMethod,
)
from .conclusion import (
    _ENGINE_ID_IN_PROSE,
    _MECHANISM_PROBLEM_TAIL,
    _REPORT_ESTABLISHED_PREFIX,
    _REPORT_ESTABLISHED_SUFFIX,
    _REPORT_MECHANISM_PREFIX,
    CONFIRMED_ESTABLISHED_BY,
    ConfidenceLevel,
    RootCauseConclusion,
    WorkingConclusion,
    established_by_for_display,
    mechanism_for_display,
    normalize_stored_report_content,
)
from .documentation import (
    DocumentationData,
    DocumentType,
    EscalationState,
    EscalationType,
    GeneratedDocument,
    JournalEntry,
)
from .evidence import (
    _CAPTURE_UPLOAD_SOURCES,
    _MINTED_PREFIX_TO_KIND,
    _MINTED_PREFIXES,
    _PASTE_UPLOAD_SOURCES,
    _SYNTHETIC_FILENAME_RE,
    DEFAULT_CASE_TITLE_RE,
    CaseEntity,
    EntityType,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    UploadedFile,
    is_default_case_title,
    is_minted_filename,
    minted_filename_phrase,
)
from .evidence_needs import (
    EvidenceNeed,
    NeedObtainability,
    NeedPriority,
    NeedPurpose,
    NeedState,
)
from .hypothesis import (
    TERMINAL_HYPOTHESIS_STATES,
    Hypothesis,
    HypothesisCategory,
    HypothesisEvidenceLink,
    HypothesisGenerationMode,
    HypothesisState,
)
from .lifecycle import (
    LEGAL_TRANSITIONS,
    VALID_CLOSURE_REASONS,
    CaseAction,
    CaseSeverity,
    CaseState,
    InvestigationStrategy,
    ParticipantRole,
    is_valid_action,
    is_valid_transition,
)
from .problem import (
    Change,
    Correlation,
    InquiryData,
    InvestigationStage,
    KnowledgeMatch,
    KnowledgeResolution,
    PreliminaryUrgency,
    ProblemConfirmation,
    ProblemVerification,
    TemporalState,
    UrgencyLevel,
)
from .progress import (
    CauseAssuranceGrade,
    CauseState,
    InvestigationProgress,
    MitigationRecord,
    SolutionFeasible,
    SolutionState,
    VerificationStatus,
)
from .solution import (
    ActionAttempt,
    InvestigationActionType,
    ProposedAction,
    Solution,
    SolutionOutcome,
    SolutionType,
    _action_type_value,
    classify_solution_outcome,
)
from .turn import (
    NON_INVESTIGATIVE_OUTCOMES,
    InvestigationMomentum,
    TurnOutcome,
    TurnProgress,
)
