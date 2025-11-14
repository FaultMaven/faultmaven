"""Team Management API Routes

Purpose: REST API endpoints for team collaboration management

This module provides REST API endpoints for managing teams within organizations,
enabling sub-organization collaboration and resource sharing.

Key Endpoints:
- Team CRUD operations
- Team member management
- Team-based access control
- Team discovery
"""

from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from faultmaven.models.interfaces_user import (
    Team,
    TeamMember
)
from faultmaven.services.domain.team_service import TeamService
from faultmaven.api.v1.auth_dependencies import (
    require_authentication,
    get_current_user_id
)
from faultmaven.exceptions import (
    ValidationException,
    ServiceException,
    NotFoundException,
    PermissionDeniedException
)
from faultmaven.container import container

# Create router
router = APIRouter(prefix="/teams", tags=["teams"])

# Set up logging
logger = logging.getLogger(__name__)


# ============================================================================
# Pydantic Models
# ============================================================================

class TeamCreateRequest(BaseModel):
    """Request to create a new team"""
    org_id: str = Field(..., description="Organization ID")
    name: str = Field(..., description="Team name", min_length=1, max_length=200)
    description: Optional[str] = Field(None, description="Team description", max_length=1000)


class TeamUpdateRequest(BaseModel):
    """Request to update team details"""
    name: Optional[str] = Field(None, description="Updated team name", max_length=200)
    description: Optional[str] = Field(None, description="Updated description", max_length=1000)
    settings: Optional[Dict[str, Any]] = Field(None, description="Team settings")


class TeamResponse(BaseModel):
    """Team details response"""
    team_id: str
    org_id: str
    name: str
    description: Optional[str]
    settings: Dict[str, Any]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TeamMemberAddRequest(BaseModel):
    """Request to add member to team"""
    user_id: str = Field(..., description="User ID to add")
    team_role: Optional[str] = Field("member", description="Team role ('lead' or 'member')")


class TeamMemberResponse(BaseModel):
    """Team member response"""
    user_id: str
    team_id: str
    team_role: Optional[str]
    joined_at: datetime

    class Config:
        from_attributes = True


# ============================================================================
# Dependencies
# ============================================================================

async def get_team_service() -> TeamService:
    """Get TeamService instance from container"""
    service = container.get_team_service()
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Team service not available"
        )
    return service


# ============================================================================
# Team Endpoints
# ============================================================================

@router.post(
    "",
    response_model=TeamResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Team",
    description="Create a new team within an organization. The creator becomes the team lead."
)
async def create_team(
    request: TeamCreateRequest = Body(...),
    user_id: str = Depends(get_current_user_id),
    service: TeamService = Depends(get_team_service)
) -> TeamResponse:
    """Create a new team with the authenticated user as team lead."""
    try:
        team = await service.create_team(
            org_id=request.org_id,
            name=request.name,
            creator_user_id=user_id,
            description=request.description
        )

        logger.info(f"Team created: {team.team_id} ({team.name}) in org {request.org_id}")

        return TeamResponse(
            team_id=team.team_id,
            org_id=team.org_id,
            name=team.name,
            description=team.description,
            settings=team.settings or {},
            created_at=team.created_at,
            updated_at=team.updated_at
        )

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ServiceException as e:
        logger.error(f"Service error creating team: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get(
    "/{team_id}",
    response_model=TeamResponse,
    summary="Get Team",
    description="Get team details by ID."
)
async def get_team(
    team_id: str = Path(..., description="Team ID"),
    user_id: str = Depends(get_current_user_id),
    service: TeamService = Depends(get_team_service)
) -> TeamResponse:
    """Get team details."""
    try:
        team = await service.get_team(team_id)

        if not team:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Team {team_id} not found"
            )

        return TeamResponse(
            team_id=team.team_id,
            org_id=team.org_id,
            name=team.name,
            description=team.description,
            settings=team.settings or {},
            created_at=team.created_at,
            updated_at=team.updated_at
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting team {team_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.put(
    "/{team_id}",
    response_model=TeamResponse,
    summary="Update Team",
    description="Update team details. Requires 'teams.write' permission."
)
async def update_team(
    team_id: str = Path(..., description="Team ID"),
    request: TeamUpdateRequest = Body(...),
    user_id: str = Depends(get_current_user_id),
    service: TeamService = Depends(get_team_service)
) -> TeamResponse:
    """Update team details."""
    try:
        success = await service.update_team(
            team_id=team_id,
            user_id=user_id,
            name=request.name,
            description=request.description,
            settings=request.settings
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Team {team_id} not found"
            )

        # Fetch updated team
        team = await service.get_team(team_id)

        logger.info(f"Team updated: {team_id} by user {user_id}")

        return TeamResponse(
            team_id=team.team_id,
            org_id=team.org_id,
            name=team.name,
            description=team.description,
            settings=team.settings or {},
            created_at=team.created_at,
            updated_at=team.updated_at
        )

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating team {team_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.delete(
    "/{team_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Team",
    description="Soft delete a team. Requires 'teams.manage' permission."
)
async def delete_team(
    team_id: str = Path(..., description="Team ID"),
    user_id: str = Depends(get_current_user_id),
    service: TeamService = Depends(get_team_service)
):
    """Soft delete a team."""
    try:
        success = await service.delete_team(team_id, user_id)

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Team {team_id} not found"
            )

        logger.info(f"Team deleted: {team_id} by user {user_id}")

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting team {team_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get(
    "/organization/{org_id}",
    response_model=List[TeamResponse],
    summary="List Organization Teams",
    description="List all teams in an organization."
)
async def list_organization_teams(
    org_id: str = Path(..., description="Organization ID"),
    user_id: str = Depends(get_current_user_id),
    service: TeamService = Depends(get_team_service)
) -> List[TeamResponse]:
    """List all teams in an organization."""
    try:
        teams = await service.list_organization_teams(org_id)

        return [
            TeamResponse(
                team_id=team.team_id,
                org_id=team.org_id,
                name=team.name,
                description=team.description,
                settings=team.settings or {},
                created_at=team.created_at,
                updated_at=team.updated_at
            )
            for team in teams
        ]

    except Exception as e:
        logger.error(f"Error listing teams for organization {org_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get(
    "/user/{target_user_id}/organization/{org_id}",
    response_model=List[TeamResponse],
    summary="List User Teams",
    description="List all teams a user belongs to in an organization."
)
async def list_user_teams(
    target_user_id: str = Path(..., description="User ID"),
    org_id: str = Path(..., description="Organization ID"),
    user_id: str = Depends(get_current_user_id),
    service: TeamService = Depends(get_team_service)
) -> List[TeamResponse]:
    """List all teams a user belongs to in an organization."""
    try:
        teams = await service.list_user_teams(target_user_id, org_id)

        return [
            TeamResponse(
                team_id=team.team_id,
                org_id=team.org_id,
                name=team.name,
                description=team.description,
                settings=team.settings or {},
                created_at=team.created_at,
                updated_at=team.updated_at
            )
            for team in teams
        ]

    except Exception as e:
        logger.error(f"Error listing teams for user {target_user_id} in org {org_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ============================================================================
# Team Member Management Endpoints
# ============================================================================

@router.post(
    "/{team_id}/members",
    response_model=TeamMemberResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add Team Member",
    description="Add user to team. Requires 'teams.write' permission."
)
async def add_team_member(
    team_id: str = Path(..., description="Team ID"),
    request: TeamMemberAddRequest = Body(...),
    user_id: str = Depends(get_current_user_id),
    service: TeamService = Depends(get_team_service)
) -> TeamMemberResponse:
    """Add member to team."""
    try:
        success = await service.add_member(
            team_id=team_id,
            user_id=request.user_id,
            added_by=user_id,
            team_role=request.team_role
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to add team member"
            )

        # Fetch the added member
        members = await service.list_team_members(team_id)
        member = next((m for m in members if m.user_id == request.user_id), None)

        if not member:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Member added but not found"
            )

        logger.info(f"Member added to team: user {request.user_id} to team {team_id}")

        return TeamMemberResponse(
            user_id=member.user_id,
            team_id=member.team_id,
            team_role=member.team_role,
            joined_at=member.joined_at
        )

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding member to team {team_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.delete(
    "/{team_id}/members/{target_user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove Team Member",
    description="Remove user from team. Requires 'teams.write' permission."
)
async def remove_team_member(
    team_id: str = Path(..., description="Team ID"),
    target_user_id: str = Path(..., description="User ID to remove"),
    user_id: str = Depends(get_current_user_id),
    service: TeamService = Depends(get_team_service)
):
    """Remove member from team."""
    try:
        success = await service.remove_member(
            team_id=team_id,
            user_id=target_user_id,
            removed_by=user_id
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Member {target_user_id} not found in team {team_id}"
            )

        logger.info(f"Member removed from team: user {target_user_id} from team {team_id}")

    except ValidationException as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing member from team {team_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get(
    "/{team_id}/members",
    response_model=List[TeamMemberResponse],
    summary="List Team Members",
    description="List all members of a team."
)
async def list_team_members(
    team_id: str = Path(..., description="Team ID"),
    user_id: str = Depends(get_current_user_id),
    service: TeamService = Depends(get_team_service)
) -> List[TeamMemberResponse]:
    """List all members of a team."""
    try:
        members = await service.list_team_members(team_id)

        return [
            TeamMemberResponse(
                user_id=member.user_id,
                team_id=member.team_id,
                team_role=member.team_role,
                joined_at=member.joined_at
            )
            for member in members
        ]

    except Exception as e:
        logger.error(f"Error listing members for team {team_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get(
    "/{team_id}/members/{target_user_id}/is-member",
    response_model=Dict[str, bool],
    summary="Check Team Membership",
    description="Check if user is member of team."
)
async def is_team_member(
    team_id: str = Path(..., description="Team ID"),
    target_user_id: str = Path(..., description="User ID"),
    user_id: str = Depends(get_current_user_id),
    service: TeamService = Depends(get_team_service)
) -> Dict[str, bool]:
    """Check if user is member of team."""
    try:
        is_member = await service.is_team_member(team_id, target_user_id)

        return {
            "is_member": is_member,
            "team_id": team_id,
            "user_id": target_user_id
        }

    except Exception as e:
        logger.error(f"Error checking membership for user {target_user_id} in team {team_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
