"""API Request/Response Models for Hypothesis and Solution Tracking (TASK-026).

This module defines Pydantic models for the Hypothesis and Solution API endpoints.
These models provide clear API contracts with validation, examples, and documentation.

Design: Follows TASK-024 (Report Module) API model pattern.
"""

from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, field_validator


# ============================================================
# Hypothesis API Models
# ============================================================


class HypothesisCreateRequest(BaseModel):
    """Request model for creating a hypothesis."""

    title: Optional[str] = Field(
        None,
        min_length=5,
        max_length=200,
        description="Short hypothesis title (optional, derived from description if not provided)",
    )

    description: str = Field(
        ...,
        min_length=10,
        max_length=5000,
        description="Hypothesis description - what we think caused the problem",
    )

    confidence_score: Optional[Decimal] = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Initial confidence score (0.00-1.00). Optional, can be set later.",
    )

    supporting_evidence_ids: List[str] = Field(
        default_factory=list,
        description="List of evidence IDs supporting this hypothesis",
    )

    metadata: Dict = Field(
        default_factory=dict,
        description="Additional metadata (source, rationale, etc.)",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "title": "Database connection pool exhausted",
                "description": "Database connection timeout due to network latency spikes between app and DB server",
                "confidence_score": 0.7,
                "supporting_evidence_ids": ["e1234567890ab"],
                "metadata": {
                    "source": "manual",
                    "rationale": "Observed 200ms+ latency in logs during incident window",
                },
            }
        }


class HypothesisUpdateRequest(BaseModel):
    """Request model for updating a hypothesis."""

    title: Optional[str] = Field(
        None,
        min_length=5,
        max_length=200,
        description="Updated hypothesis title",
    )

    description: Optional[str] = Field(
        None,
        min_length=10,
        max_length=5000,
        description="Updated hypothesis description",
    )

    status: Optional[str] = Field(
        None,
        description="Status: proposed, testing, validated, invalidated, deferred",
    )

    confidence: Optional[float] = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Updated confidence score (0.00-1.00)",
    )

    evidence_supporting: Optional[List[str]] = Field(
        None,
        description="Updated list of supporting evidence IDs",
    )

    evidence_against: Optional[List[str]] = Field(
        None,
        description="Updated list of contradicting evidence IDs",
    )

    validation_result: Optional[str] = Field(
        None,
        max_length=2000,
        description="Validation result or findings",
    )

    metadata: Optional[Dict] = Field(
        None,
        description="Updated metadata",
    )

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v):
        """Validate confidence score range."""
        if v is not None and (v < 0 or v > 1):
            raise ValueError("Confidence score must be between 0.00 and 1.00")
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "status": "validated",
                "confidence_score": 0.95,
                "validation_result": "Network trace confirmed 250ms average latency to DB server. Validated via tcpdump analysis.",
            }
        }


class HypothesisValidateRequest(BaseModel):
    """Request model for validating a hypothesis."""

    confidence_score: Decimal = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Validation confidence score (0.00-1.00)",
    )

    validation_method: str = Field(
        ...,
        min_length=5,
        max_length=200,
        description="How was this hypothesis validated?",
    )

    validation_result: str = Field(
        ...,
        min_length=10,
        max_length=2000,
        description="Detailed validation findings",
    )

    supporting_evidence_ids: Optional[List[str]] = Field(
        default_factory=list,
        description="Evidence IDs that validate this hypothesis",
    )

    @field_validator("confidence_score")
    @classmethod
    def validate_confidence(cls, v):
        """Validate confidence score range."""
        if v < 0 or v > 1:
            raise ValueError("Confidence score must be between 0.00 and 1.00")
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "confidence_score": 0.92,
                "validation_method": "Network performance analysis (tcpdump + iperf3)",
                "validation_result": "Confirmed average 250ms RTT to DB server. Packet loss 0.2% during peak hours. Validates network latency hypothesis.",
                "supporting_evidence_ids": ["e1234567890ab", "e2345678901bc"],
            }
        }


class HypothesisResponse(BaseModel):
    """Response model for a hypothesis."""

    hypothesis_id: str = Field(..., description="Unique hypothesis identifier")
    case_id: str = Field(..., description="Associated case identifier")
    description: str = Field(..., description="Hypothesis description")
    status: str = Field(..., description="Current status")
    confidence_score: Optional[float] = Field(
        None, description="Confidence score (0.00-1.00)"
    )
    supporting_evidence_ids: List[str] = Field(
        ..., description="Supporting evidence IDs"
    )
    validation_result: Optional[str] = Field(None, description="Validation result")
    validation_timestamp: Optional[datetime] = Field(None, description="When validated")
    proposed_at: datetime = Field(..., description="When proposed")
    updated_at: datetime = Field(..., description="Last updated")
    created_by: str = Field(..., description="User who created this hypothesis")
    updated_by: Optional[str] = Field(None, description="User who last updated")
    metadata: Dict = Field(default_factory=dict, description="Additional metadata")

    @classmethod
    def from_dict(cls, data: Dict) -> "HypothesisResponse":
        """Create HypothesisResponse from dictionary (repository layer).

        Use from_domain() instead for domain objects. This method is for
        direct dictionary sources (e.g., database results).
        """
        return cls(
            hypothesis_id=data.get("hypothesis_id"),
            case_id=data.get("case_id"),
            description=data.get("description"),
            status=data.get("status"),
            confidence_score=(
                float(data.get("confidence_score"))
                if data.get("confidence_score") is not None
                else None
            ),
            supporting_evidence_ids=data.get("supporting_evidence_ids", []),
            validation_result=data.get("validation_result"),
            validation_timestamp=data.get("validation_timestamp"),
            proposed_at=data.get("proposed_at"),
            updated_at=data.get("updated_at"),
            created_by=data.get("created_by"),
            updated_by=data.get("updated_by"),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_domain(cls, data: Dict) -> "HypothesisResponse":
        """Create HypothesisResponse from domain model dict (orchestrator/repository layer).

        This method is consistent with TASK-024 (Report Module) pattern.
        The orchestrator returns dicts, so this is just an alias for from_dict().

        Args:
            data: Dictionary from orchestrator or repository

        Returns:
            HypothesisResponse instance
        """
        return cls.from_dict(data)

    class Config:
        from_attributes = True


# ============================================================
# Solution API Models
# ============================================================


class SolutionCreateRequest(BaseModel):
    """Request model for creating a solution."""

    title: str = Field(
        ...,
        min_length=5,
        max_length=200,
        description="Short solution title",
    )

    description: str = Field(
        ...,
        min_length=20,
        max_length=5000,
        description="Detailed solution description",
    )

    implementation_steps: List[str] = Field(
        ...,
        min_items=1,
        description="Ordered list of implementation steps",
    )

    hypothesis_id: Optional[str] = Field(
        None,
        description="Optional hypothesis ID to link this solution to (must be validated)",
    )

    risk_level: Optional[str] = Field(
        None,
        description="Risk level: low, medium, high, critical",
    )

    estimated_effort: Optional[str] = Field(
        None,
        max_length=100,
        description="Estimated effort (e.g., '2 hours', '1 day', 'Immediate')",
    )

    metadata: Dict = Field(
        default_factory=dict,
        description="Additional metadata (rollback plan, dependencies, etc.)",
    )

    @field_validator("risk_level")
    @classmethod
    def validate_risk_level(cls, v):
        """Validate risk level values."""
        if v and v not in ["low", "medium", "high", "critical"]:
            raise ValueError("Risk level must be: low, medium, high, or critical")
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "title": "Increase database connection timeout",
                "description": "Increase database connection timeout from 5s to 30s to accommodate network latency",
                "implementation_steps": [
                    "Backup current database.yml configuration",
                    "Edit database.yml: timeout: 30000 (30 seconds)",
                    "Restart application server (zero-downtime rolling restart)",
                    "Monitor connection error rate for 24 hours",
                    "Verify P95 latency metrics improved",
                ],
                "hypothesis_id": "hyp-123",
                "risk_level": "low",
                "estimated_effort": "30 minutes",
                "metadata": {
                    "rollback_plan": "Revert database.yml to backup, restart server",
                    "dependencies": ["Requires application restart"],
                },
            }
        }


class SolutionUpdateRequest(BaseModel):
    """Request model for updating a solution."""

    description: Optional[str] = Field(
        None,
        min_length=20,
        max_length=5000,
        description="Updated solution description",
    )

    status: Optional[str] = Field(
        None,
        description="Status: proposed, in_progress, implemented, verified, rejected",
    )

    implementation_steps: Optional[List[str]] = Field(
        None,
        min_items=1,
        description="Updated implementation steps",
    )

    risk_level: Optional[str] = Field(
        None,
        description="Updated risk level: low, medium, high, critical",
    )

    estimated_effort: Optional[str] = Field(
        None,
        max_length=100,
        description="Updated estimated effort",
    )

    verification_result: Optional[str] = Field(
        None,
        max_length=2000,
        description="Verification result after implementation",
    )

    implemented: Optional[bool] = Field(
        None,
        description="Mark as implemented (sets implemented_at timestamp)",
    )

    metadata: Optional[Dict] = Field(
        None,
        description="Updated metadata",
    )

    @field_validator("risk_level")
    @classmethod
    def validate_risk_level(cls, v):
        """Validate risk level values."""
        if v and v not in ["low", "medium", "high", "critical"]:
            raise ValueError("Risk level must be: low, medium, high, or critical")
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "status": "implemented",
                "implemented": True,
                "verification_result": "Connection timeout errors dropped from 120/hour to 0. P95 latency improved from 450ms to 280ms. Solution verified successful.",
            }
        }


class SolutionResponse(BaseModel):
    """Response model for a solution."""

    solution_id: str = Field(..., description="Unique solution identifier")
    case_id: str = Field(..., description="Associated case identifier")
    hypothesis_id: Optional[str] = Field(
        None, description="Linked hypothesis identifier (if any)"
    )
    description: str = Field(..., description="Solution description")
    status: str = Field(..., description="Current status")
    implementation_steps: List[str] = Field(..., description="Implementation steps")
    risk_level: Optional[str] = Field(None, description="Risk level")
    estimated_effort: Optional[str] = Field(None, description="Estimated effort")
    verification_result: Optional[str] = Field(None, description="Verification result")
    verification_timestamp: Optional[datetime] = Field(
        None, description="When verified"
    )
    proposed_at: datetime = Field(..., description="When proposed")
    implemented_at: Optional[datetime] = Field(None, description="When implemented")
    updated_at: datetime = Field(..., description="Last updated")
    created_by: str = Field(..., description="User who created this solution")
    updated_by: Optional[str] = Field(None, description="User who last updated")
    metadata: Dict = Field(default_factory=dict, description="Additional metadata")

    @classmethod
    def from_dict(cls, data: Dict) -> "SolutionResponse":
        """Create SolutionResponse from dictionary (repository layer)."""
        # Extract metadata fields
        metadata = data.get("metadata", {})

        # implementation_steps can be either top-level (from domain model) or in metadata (from repository)
        implementation_steps = data.get("implementation_steps")
        if implementation_steps is None:
            implementation_steps = metadata.get("implementation_steps", [])

        # risk_level and estimated_effort can also be either top-level or in metadata
        risk_level = data.get("risk_level")
        if risk_level is None:
            risk_level = metadata.get("risk_level")

        estimated_effort = data.get("estimated_effort")
        if estimated_effort is None:
            estimated_effort = metadata.get("estimated_effort")

        return cls(
            solution_id=data.get("solution_id"),
            case_id=data.get("case_id"),
            hypothesis_id=data.get("hypothesis_id"),
            description=data.get("description"),
            status=data.get("status"),
            implementation_steps=implementation_steps,
            risk_level=risk_level,
            estimated_effort=estimated_effort,
            verification_result=data.get("verification_result"),
            verification_timestamp=data.get("verification_timestamp"),
            proposed_at=data.get("proposed_at"),
            implemented_at=data.get("implemented_at"),
            updated_at=data.get("updated_at"),
            created_by=data.get("created_by"),
            updated_by=data.get("updated_by"),
            metadata=metadata,
        )

    @classmethod
    def from_domain(cls, data: Dict) -> "SolutionResponse":
        """Create SolutionResponse from domain model (orchestrator/repository layer).

        This is an alias for from_dict() for consistency with other API response models.
        """
        return cls.from_dict(data)

    class Config:
        from_attributes = True


# ============================================================
# Investigation Progress Models
# ============================================================


class InvestigationProgressResponse(BaseModel):
    """Response model for investigation progress summary."""

    hypotheses: Dict = Field(
        ...,
        description="Hypothesis metrics (total, confirmed, rejected, testing, completion_rate)",
    )

    solutions: Dict = Field(
        ...,
        description="Solution metrics (total, implemented, implementation_rate)",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "hypotheses": {
                    "total": 5,
                    "proposed": 1,
                    "testing": 2,
                    "validated": 1,
                    "invalidated": 1,
                    "deferred": 0,
                    "completion_rate": 40.0,
                },
                "solutions": {
                    "total": 2,
                    "implemented": 1,
                    "implementation_rate": 50.0,
                },
            }
        }
