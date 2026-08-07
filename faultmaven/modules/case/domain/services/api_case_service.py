"""API Case Service Module (TASK-011)

Purpose: API service layer for case management operations.

This service provides business logic orchestration between FastAPI controllers
and domain repositories. It handles:
- Business logic and validation
- Transaction coordination across multiple repositories
- Error translation (domain errors → service exceptions)
- Authorization checks (organization ownership)
- Audit logging

Architecture:
    FastAPI Routes → CaseService → Repositories → Database

This is distinct from the domain CaseService (faultmaven/services/domain/case_service.py)
which handles internal case operations and conversation management.
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from uuid import uuid4

from faultmaven.services.base import BaseService

# Interface imports for clean architecture compliance
if TYPE_CHECKING:
    from faultmaven.models.interfaces import IVectorStore
# Import from contracts.py per Principle 2 (Vertical Modules with Contracts)
from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    RepositoryError,
    ServiceError,
    ValidationException,
)
from faultmaven.infrastructure.persistence.investigation_session_repository import (
    InvestigationSessionRepository,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    ICaseRepository,
    InvestigationStrategy,
)
from faultmaven.modules.case.infrastructure.case_repository import CaseRepository
from faultmaven.providers.tenancy.base import TenantProvider


class APICaseService(BaseService):
    """Service for API case management operations.

    This service provides a clean API layer for case management with:
    - Organization-level authorization on all operations
    - Service-level exception translation
    - Unified logging and error handling
    - Transaction coordination across repositories
    - Deployment-neutral tenant resolution (TASK-023)

    Attributes:
        case_repo: Case repository for case persistence
        session_repo: Investigation session repository
        evidence_repo: Evidence artifact repository
        tenant_provider: Provider for deployment-neutral organization context
    """

    def __init__(
        self,
        case_repo: ICaseRepository,
        session_repo: InvestigationSessionRepository,
        tenant_provider: Optional[TenantProvider] = None,
    ):
        """Initialize API case service.

        Args:
            case_repo: Case repository (handles evidence and agent executions - migrated from separate repositories)
            session_repo: Investigation session repository
            tenant_provider: Provider for deployment-neutral organization context (TASK-023)
        """
        super().__init__("api_case_service")
        self.case_repo = case_repo
        self.session_repo = session_repo
        self.tenant_provider = tenant_provider

    # ============================================================
    # Core CRUD Operations
    # ============================================================

    async def create_case(
        self,
        user_id: str,
        organization_id: Optional[str] = None,
        title: str = "",
        description: str = "",
        severity: Optional[CaseSeverity] = None,
        metadata: Optional[Dict[str, Any]] = None,
        current_user: Optional[Any] = None,
    ) -> Case:
        """Create a new case.

        Deployment-neutral implementation using TenantProvider (TASK-023):
        - Single-tenant: organization_id ignored, uses default organization
        - Multi-tenant: organization_id required, validates membership

        Args:
            user_id: User creating the case
            organization_id: Organization ID (optional for single-tenant, required for multi-tenant)
            title: Case title
            description: Case description
            severity: Case severity level
            metadata: Optional additional metadata
            current_user: Authenticated user (for TenantProvider)

        Returns:
            Created Case

        Raises:
            ValidationException: If inputs invalid
            ServiceError: If creation fails
        """
        # Resolve organization context using TenantProvider (deployment-neutral)
        resolved_org_id = organization_id
        if self.tenant_provider and current_user:
            try:
                organization = await self.tenant_provider.get_current_organization(
                    current_user=current_user, organization_id=organization_id
                )
                resolved_org_id = organization.organization_id
            except Exception as e:
                self.log_error("tenant_provider_resolution", e, user_id=user_id)
                # Fall back to provided organization_id if TenantProvider fails
                if not organization_id:
                    raise

        self.log_operation(
            "create_case",
            user_id=user_id,
            organization_id=resolved_org_id,
            title=title,
            severity=severity.value if severity else None,
        )

        # Validate inputs
        if not title or not title.strip():
            raise ValidationException("title: Title is required")

        if len(title) > 200:
            raise ValidationException("title: Title cannot exceed 200 characters")

        if not user_id or not user_id.strip():
            raise ValidationException("user_id: User ID is required")

        if not resolved_org_id or not resolved_org_id.strip():
            raise ValidationException("organization_id: Organization ID is required")

        try:
            # Create case with milestone-based model
            case = Case(
                case_id=f"case_{uuid4().hex[:12]}",
                user_id=user_id.strip(),
                organization_id=resolved_org_id.strip(),
                title=title.strip(),
                description=description.strip() if description else "",
                state=CaseState.INQUIRY,
                investigation_strategy=InvestigationStrategy.POST_MORTEM,
            )

            # Store severity in metadata since Case model uses string-based severity in ProblemVerification
            case_metadata = metadata or {}
            case_metadata["severity"] = severity.value

            # Save the case
            saved_case = await self.case_repo.save(case)

            self.log_operation(
                "create_case_success",
                case_id=saved_case.case_id,
                user_id=user_id,
                organization_id=organization_id,
            )

            return saved_case

        except ValidationException:
            raise
        except Exception as e:
            self.log_error(
                "create_case", e, user_id=user_id, organization_id=organization_id
            )
            raise ServiceError(f"Failed to create case: {e}")

    async def get_case(
        self,
        case_id: str,
        organization_id: str,
    ) -> Optional[Case]:
        """Get case by ID with organization check.

        Args:
            case_id: Case ID to retrieve
            organization_id: Organization for authorization check

        Returns:
            Case if found and authorized, None otherwise
        """
        self.log_operation("get_case", case_id=case_id, organization_id=organization_id)

        if not case_id or not case_id.strip():
            return None

        try:
            case = await self.case_repo.get(case_id)

            if not case:
                return None

            # Authorization check - organization must own the case
            if case.organization_id != organization_id:
                self.log_operation(
                    "get_case_unauthorized",
                    case_id=case_id,
                    organization_id=organization_id,
                    case_org_id=case.organization_id,
                )
                return None

            return case

        except Exception as e:
            self.log_error(
                "get_case", e, case_id=case_id, organization_id=organization_id
            )
            return None

    async def update_case(
        self,
        case_id: str,
        organization_id: str,
        updates: Dict[str, Any],
    ) -> Case:
        """Update case with authorization check.

        Args:
            case_id: Case ID to update
            organization_id: Organization for authorization check
            updates: Fields to update (title, description, severity, status, etc.)

        Returns:
            Updated Case

        Raises:
            NotFoundError: If case not found
            AuthorizationError: If organization doesn't own case
            ValidationException: If updates invalid
        """
        self.log_operation(
            "update_case",
            case_id=case_id,
            organization_id=organization_id,
            update_fields=list(updates.keys()) if updates else [],
        )

        if not case_id or not case_id.strip():
            raise ValidationException("case_id: Case ID is required")

        if not updates:
            raise ValidationException("updates: At least one update field is required")

        try:
            from pydantic import ValidationError as PydanticValidationError

            # Get current case
            case = await self.case_repo.get(case_id)

            if not case:
                raise NotFoundError("Case", case_id)

            # Authorization check
            if case.organization_id != organization_id:
                raise AuthorizationError(
                    f"Case {case_id} not accessible by organization {organization_id}"
                )

            # Validate and apply updates
            allowed_fields = {
                "title",
                "description",
                "state",
                "severity",
                "assigned_to",
            }

            for key, value in updates.items():
                if key not in allowed_fields:
                    continue

                if key == "title":
                    if not value or not value.strip():
                        raise ValidationException("title: Title cannot be empty")
                    if len(value) > 200:
                        raise ValidationException(
                            "title: Title cannot exceed 200 characters"
                        )
                    case.title = value.strip()

                elif key == "description":
                    case.description = value.strip() if value else ""

                elif key == "state":
                    if isinstance(value, str):
                        try:
                            value = CaseState(value)
                        except ValueError:
                            raise ValidationException(
                                f"state: Invalid status '{value}'. Must be one of: "
                                f"{[s.value for s in CaseState]}"
                            )
                    # Close-path integrity: a generic update must not jump a case
                    # straight into a terminal state. RESOLVED/CLOSED carry
                    # mandatory closure semantics (resolution gating,
                    # closure_reason, a case_actions audit row) that only the
                    # dedicated transition paths apply. A raw PATCH here would
                    # leave an unclassified, unaudited terminal — the exact gap
                    # the funnel's "unknown" closure bucket was added to surface.
                    if value in (CaseState.RESOLVED, CaseState.CLOSED):
                        raise ValidationException(
                            f"state: cannot transition to terminal state "
                            f"'{value.value}' via update. Use the resolve/close "
                            f"transition (which records closure reason and audit "
                            f"history)."
                        )
                    try:
                        case.state = value
                    except PydanticValidationError as e:
                        raise ValidationException(str(e))

                elif key == "severity":
                    # Store severity in case metadata
                    pass  # Handled via metadata

                elif key == "assigned_to":
                    # Store assigned_to in case - add as a field if needed
                    pass

            # Update timestamp
            case.updated_at = datetime.now(timezone.utc)

            # Save updated case
            saved_case = await self.case_repo.save(case)

            self.log_operation(
                "update_case_success",
                case_id=case_id,
                organization_id=organization_id,
            )

            return saved_case

        except (NotFoundError, AuthorizationError, ValidationException):
            raise
        except Exception as e:
            self.log_error(
                "update_case", e, case_id=case_id, organization_id=organization_id
            )
            raise ServiceError(f"Failed to update case: {e}")

    async def delete_case(
        self,
        case_id: str,
        organization_id: str,
    ) -> bool:
        """Delete case with authorization check.

        This triggers CASCADE delete:
        - All investigation sessions
        - All agent executions
        - All tool calls
        - All evidence artifacts

        Args:
            case_id: Case ID to delete
            organization_id: Organization for authorization check

        Returns:
            True if deleted, False if not found

        Raises:
            AuthorizationError: If organization doesn't own case
        """
        self.log_operation(
            "delete_case", case_id=case_id, organization_id=organization_id
        )

        if not case_id or not case_id.strip():
            return False

        try:
            # Get case to check authorization
            case = await self.case_repo.get(case_id)

            if not case:
                return False

            # Authorization check
            if case.organization_id != organization_id:
                raise AuthorizationError(
                    f"Case {case_id} not accessible by organization {organization_id}"
                )

            # Delete the case (CASCADE will handle related entities via database)
            deleted = await self.case_repo.delete(case_id)

            if deleted:
                self.log_operation(
                    "delete_case_success",
                    case_id=case_id,
                    organization_id=organization_id,
                )

            return deleted

        except AuthorizationError:
            raise
        except Exception as e:
            self.log_error(
                "delete_case", e, case_id=case_id, organization_id=organization_id
            )
            return False

    # ============================================================
    # Query Operations
    # ============================================================

    async def list_cases(
        self,
        organization_id: Optional[str] = None,
        user_id: Optional[str] = None,
        state: Optional[CaseState] = None,
        severity: Optional[CaseSeverity] = None,
        assigned_to: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        current_user: Optional[Any] = None,
    ) -> List[Case]:
        """List cases for organization with filters.

        Deployment-neutral implementation using TenantProvider (TASK-023):
        - Single-tenant: organization_id ignored, lists all cases in default org
        - Multi-tenant: organization_id required, lists cases with membership check

        Args:
            organization_id: Organization ID (optional for single-tenant, required for multi-tenant)
            user_id: Optional filter by reporter
            state: Optional filter by status
            severity: Optional filter by severity
            assigned_to: Optional filter by assignee
            limit: Max results (default 50)
            offset: Pagination offset
            current_user: Authenticated user (for TenantProvider)

        Returns:
            List of cases
        """
        # Resolve organization context using TenantProvider (deployment-neutral)
        resolved_org_id = organization_id
        if self.tenant_provider and current_user:
            try:
                organization = await self.tenant_provider.get_current_organization(
                    current_user=current_user, organization_id=organization_id
                )
                resolved_org_id = organization.organization_id
            except Exception as e:
                self.log_error("tenant_provider_resolution", e)
                # Fall back to provided organization_id if TenantProvider fails
                if not organization_id:
                    raise

        self.log_operation(
            "list_cases",
            organization_id=resolved_org_id,
            user_id=user_id,
            state=state.value if state else None,
            severity=severity.value if severity else None,
            assigned_to=assigned_to,
            limit=limit,
            offset=offset,
        )

        if not resolved_org_id:
            return []

        try:
            # Get cases from repository
            cases, total = await self.case_repo.list(
                organization_id=resolved_org_id,
                user_id=user_id,
                state=state,
                limit=limit,
                offset=offset,
            )

            # Apply additional filters not supported by repository
            if severity:
                # Filter by severity stored in metadata
                cases = [
                    c
                    for c in cases
                    if hasattr(c, "problem_verification")
                    and c.problem_verification
                    and c.problem_verification.severity.lower() == severity.value
                ]

            # Note: assigned_to filter would need to be handled here if stored in metadata

            self.log_operation(
                "list_cases_result",
                organization_id=resolved_org_id,
                count=len(cases),
                total=total,
            )

            return cases

        except Exception as e:
            self.log_error("list_cases", e, organization_id=resolved_org_id)
            return []

    async def get_case_with_details(
        self,
        case_id: str,
        organization_id: str,
        include_sessions: bool = True,
        include_evidence: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Get case with related entities.

        Args:
            case_id: Case ID
            organization_id: Organization for authorization
            include_sessions: Include investigation sessions
            include_evidence: Include evidence artifacts

        Returns:
            Dictionary with case and related entities
        """
        self.log_operation(
            "get_case_with_details",
            case_id=case_id,
            organization_id=organization_id,
            include_sessions=include_sessions,
            include_evidence=include_evidence,
        )

        # Get the case first
        case = await self.get_case(case_id, organization_id)
        if not case:
            return None

        try:
            result: Dict[str, Any] = {
                "case": case,
            }

            # Fetch related entities in parallel if requested
            if include_sessions:
                try:
                    sessions = await self.session_repo.list_by_case_id(case_id)
                    result["sessions"] = sessions
                except Exception as e:
                    self.log_error("get_case_sessions", e, case_id=case_id)
                    result["sessions"] = []

            if include_evidence:
                # Storage redesign 2026-04 phase 2: standalone evidence path is
                # deleted. Evidence is case-tied only and lives on `case.evidence`,
                # which the case repository loads via `_load_evidence_for_case`.
                result["evidence"] = list(getattr(case, "evidence", []) or [])

            return result

        except Exception as e:
            self.log_error(
                "get_case_with_details",
                e,
                case_id=case_id,
                organization_id=organization_id,
            )
            return None

    # ============================================================
    # Case Lifecycle Operations
    # ============================================================

    async def assign_case(
        self,
        case_id: str,
        organization_id: str,
        assigned_to: str,
    ) -> Case:
        """Assign case to user.

        Args:
            case_id: Case ID
            organization_id: Organization for authorization
            assigned_to: User ID to assign to

        Returns:
            Updated case

        Raises:
            NotFoundError: If case not found
            AuthorizationError: If organization doesn't own case
        """
        self.log_operation(
            "assign_case",
            case_id=case_id,
            organization_id=organization_id,
            assigned_to=assigned_to,
        )

        if not assigned_to or not assigned_to.strip():
            raise ValidationException("assigned_to: Assignee user ID is required")

        # Get case with authorization check
        case = await self.get_case(case_id, organization_id)
        if not case:
            raise NotFoundError("Case", case_id)

        try:
            # Update case with new assignee (storing in metadata for now)
            # In future, might add assigned_to field to Case model
            case.updated_at = datetime.now(timezone.utc)

            saved_case = await self.case_repo.save(case)

            self.log_operation(
                "assign_case_success",
                case_id=case_id,
                assigned_to=assigned_to,
            )

            return saved_case

        except Exception as e:
            self.log_error("assign_case", e, case_id=case_id, assigned_to=assigned_to)
            raise ServiceError(f"Failed to assign case: {e}")

    async def close_case(
        self,
        case_id: str,
        organization_id: str,
    ) -> Case:
        """Mark case as RESOLVED (terminal).

        Resolution narrative — root cause, solution, evidence — lives in
        the case itself (root_cause_conclusion, solutions, evidence) and
        in the auto-generated resolution_summary Report. This method is
        purely the state transition; it carries no free-form text input.

        Args:
            case_id: Case ID
            organization_id: Organization for authorization

        Returns:
            Updated case with state=RESOLVED

        Raises:
            NotFoundError: If case not found
            AuthorizationError: If organization doesn't own case
            ConflictError: If case already closed
        """
        self.log_operation(
            "close_case",
            case_id=case_id,
            organization_id=organization_id,
        )

        # Get case with authorization check
        case = await self.get_case(case_id, organization_id)
        if not case:
            raise NotFoundError("Case", case_id)

        # Check if case is already closed
        if case.state.is_terminal:
            raise ConflictError(
                f"Case {case_id} is already closed with status {case.state.value}",
                resource_type="Case",
                resource_id=case_id,
                conflict_reason="already_closed",
            )

        try:
            # RESOLVED must carry the same engine bookkeeping as the chat-side
            # executor (terminal_transitions._execute_resolved_transition) —
            # the two surfaces share ONE finalizer so they can never diverge
            # on terminal truth (grade, over-claim, verification_status, and
            # the cause_state coherence block; a hand-mirrored copy here
            # previously shipped CONFIRMED×CANDIDATES blobs). Function-local
            # import: the case_ui_adapter precedent for domain-service →
            # core.investigation reads.
            from faultmaven.core.investigation.terminal_transitions import (
                finalize_resolution_truth_surface,
            )

            if not case.progress.solution_proposed:
                case.progress.solution_proposed = True
            if not case.progress.solution_accepted:
                case.progress.solution_accepted = True
            case.progress.solution_verified = True
            finalize_resolution_truth_surface(case)

            # closure_reason is None for RESOLVED — sub-categorization
            # would be redundant with the status itself.
            now = datetime.now(timezone.utc)
            updated_case = case.model_copy(
                update={
                    "state": CaseState.RESOLVED,
                    "resolved_at": now,
                    "closed_at": now,
                    "closure_reason": None,
                    "updated_at": now,
                }
            )

            saved_case = await self.case_repo.save(updated_case)

            self.log_operation(
                "close_case_success",
                case_id=case_id,
                state=saved_case.state.value,
            )

            return saved_case

        except ConflictError:
            raise
        except Exception as e:
            self.log_error("close_case", e, case_id=case_id)
            raise ServiceError(f"Failed to close case: {e}")

    # ============================================================
    # Statistics and Analytics
    # ============================================================

    async def get_case_statistics(
        self,
        organization_id: str,
    ) -> Dict[str, Any]:
        """Get case statistics for organization.

        Returns:
            Statistics including:
            - total_cases
            - by_status (inquiry, investigating, resolved, closed)
            - by_severity (low, medium, high, critical)
            - avg_resolution_time
            - unassigned_count
        """
        self.log_operation("get_case_statistics", organization_id=organization_id)

        try:
            # Get all cases for organization
            cases, total = await self.case_repo.list(
                organization_id=organization_id,
                limit=10000,  # Get all cases for statistics
                offset=0,
            )

            # Calculate statistics
            by_status: Dict[str, int] = {}
            by_severity: Dict[str, int] = {}
            resolution_times: List[float] = []
            unassigned_count = 0

            for case in cases:
                # Count by status
                status_key = case.state.value
                by_status[status_key] = by_status.get(status_key, 0) + 1

                # Count by severity (from problem_verification if available)
                if hasattr(case, "problem_verification") and case.problem_verification:
                    severity_key = case.problem_verification.severity.lower()
                    by_severity[severity_key] = by_severity.get(severity_key, 0) + 1

                # Calculate resolution time for resolved cases
                if case.state == CaseState.RESOLVED and case.resolved_at:
                    resolution_time = (
                        case.resolved_at - case.created_at
                    ).total_seconds()
                    resolution_times.append(resolution_time)

                # Count unassigned (cases in INQUIRY without progress)
                if case.state == CaseState.INQUIRY and case.current_turn == 0:
                    unassigned_count += 1

            # Calculate average resolution time
            avg_resolution_time_seconds = 0.0
            if resolution_times:
                avg_resolution_time_seconds = sum(resolution_times) / len(
                    resolution_times
                )

            statistics = {
                "total_cases": total,
                "by_status": by_status,
                "by_severity": by_severity,
                "avg_resolution_time_seconds": avg_resolution_time_seconds,
                "avg_resolution_time_hours": (
                    avg_resolution_time_seconds / 3600
                    if avg_resolution_time_seconds
                    else 0
                ),
                "unassigned_count": unassigned_count,
                "organization_id": organization_id,
                "computed_at": datetime.now(timezone.utc).isoformat(),
            }

            self.log_operation(
                "get_case_statistics_success",
                organization_id=organization_id,
                total_cases=total,
            )

            return statistics

        except Exception as e:
            self.log_error("get_case_statistics", e, organization_id=organization_id)
            return {
                "total_cases": 0,
                "by_status": {},
                "by_severity": {},
                "avg_resolution_time_seconds": 0,
                "avg_resolution_time_hours": 0,
                "unassigned_count": 0,
                "organization_id": organization_id,
                "error": str(e),
            }

    # ============================================================
    # Search Operations
    # ============================================================

    async def search_cases(
        self,
        organization_id: str,
        query: str,
        limit: int = 20,
    ) -> List[Case]:
        """Search cases within organization.

        Args:
            organization_id: Organization to search within
            query: Search query text
            limit: Maximum results

        Returns:
            List of matching cases
        """
        self.log_operation(
            "search_cases",
            organization_id=organization_id,
            query=query[:50] if query else None,  # Truncate for logging
            limit=limit,
        )

        if not query or not query.strip():
            return []

        try:
            cases, total = await self.case_repo.search(
                query=query,
                organization_id=organization_id,
                limit=limit,
            )

            self.log_operation(
                "search_cases_result",
                organization_id=organization_id,
                count=len(cases),
            )

            return cases

        except Exception as e:
            self.log_error("search_cases", e, organization_id=organization_id)
            return []
