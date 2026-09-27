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
