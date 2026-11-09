"""The runbook gate's verdict and score (``runbook_validator``).

Apart from ``conversion``, which imports ``modules.case``: the gate's worker
imports these, and that chain cost each start about 0.4 s (#2661).
"""

from typing import List

from pydantic import BaseModel, Field


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
