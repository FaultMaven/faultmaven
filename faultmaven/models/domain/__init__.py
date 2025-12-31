"""Domain models for FaultMaven entities.

This package contains domain model classes that represent business entities
in a framework-agnostic way. These models are used for:

- API layer (request/response handling)
- Service layer (business logic)
- Testing (isolated unit tests)

They are separate from ORM models to maintain clean architecture and
testability.
"""

from faultmaven.models.domain.hypothesis import Hypothesis, HypothesisStatus
from faultmaven.models.domain.solution import Solution, SolutionStatus

__all__ = [
    "Hypothesis",
    "HypothesisStatus",
    "Solution",
    "SolutionStatus",
]
