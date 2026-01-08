"""Legacy Evidence API Models (Stub for OODA Converter Compatibility).

NOTE: This file contains stub models to support the legacy OODA response converter.
The actual evidence models are now in:
- faultmaven.modules.evidence.domain.models (domain models)
- faultmaven.modules.case.domain.models (EvidenceCategory, Evidence, etc.)

These stubs exist solely to prevent import errors in ooda_response_converter.py
which will be refactored to use the new module structure.
"""

from typing import List
from pydantic import BaseModel, Field

# Import the canonical EvidenceCategory from case models
from faultmaven.modules.case.domain.models import EvidenceCategory


class AcquisitionGuidance(BaseModel):
    """Guidance on how to acquire evidence.

    Legacy model used by OODA converter to structure evidence requests.
    """
    commands: List[str] = Field(default_factory=list, description="Commands to run")
    file_locations: List[str] = Field(default_factory=list, description="Files to collect")
    ui_locations: List[str] = Field(default_factory=list, description="UI elements to capture")


class EvidenceRequest(BaseModel):
    """API Evidence Request Model.

    Legacy model used by OODA converter for evidence requests.
    Will be replaced by EvidenceRequestToAdd from llm_schemas.py.
    """
    label: str = Field(..., description="Short label for the request", max_length=100)
    description: str = Field(..., description="Detailed description", max_length=500)
    category: EvidenceCategory = Field(..., description="Evidence category")
    guidance: AcquisitionGuidance = Field(..., description="How to acquire the evidence")
    created_at_turn: int = Field(default=0, description="Turn number when created")


# Re-export EvidenceCategory for backward compatibility
__all__ = ["EvidenceRequest", "EvidenceCategory", "AcquisitionGuidance"]
