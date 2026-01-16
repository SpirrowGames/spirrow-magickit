"""Phase 2 API routes for Magickit.

Includes routes for:
- Authentication (register, login, refresh)
- Workspaces
- Projects
- Locks
- Webhooks
- Dashboard
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from magickit.api.models import (
    DashboardStats,
    EventType,
    LockAcquire,
    LockResponse,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
    TaskEventResponse,
    TaskResponse,
    TaskStatus,
    TokenResponse,
    UserCreate,
    UserLogin,
    UserResponse,
    UserRole,
    WebhookCreate,
    WebhookResponse,
    WebhookUpdate,
    WorkspaceCreate,
    WorkspaceMemberAdd,
    WorkspaceMemberResponse,
    WorkspaceResponse,
    WorkspaceUpdate,
)
from magickit.auth.dependencies import (
    AdminUser,
    CurrentUser,
    CurrentUserId,
    OptionalUser,
    get_current_user,
)
from magickit.utils.logging import get_logger

if TYPE_CHECKING:
    from magickit.auth.jwt import JWTHandler
    from magickit.core.lock_manager import LockManager
    from magickit.core.project_manager import ProjectManager
    from magickit.core.state_manager import StateManager
    from magickit.core.workspace_manager import WorkspaceManager

logger = get_logger(__name__)

router = APIRouter()

# These will be set during app initialization
_state_manager: StateManager | None = None
_jwt_handler: JWTHandler | None = None
_workspace_manager: WorkspaceManager | None = None
_project_manager: ProjectManager | None = None
_lock_manager: LockManager | None = None


def set_v2_dependencies(
    state_manager: StateManager,
    jwt_handler: JWTHandler,
    workspace_manager: WorkspaceManager,
    project_manager: ProjectManager,
    lock_manager: LockManager,
) -> None:
    """Set Phase 2 router dependencies."""
    global _state_manager, _jwt_handler, _workspace_manager, _project_manager, _lock_manager
    _state_manager = state_manager
    _jwt_handler = jwt_handler
    _workspace_manager = workspace_manager
    _project_manager = project_manager
    _lock_manager = lock_manager


def get_state_manager() -> StateManager:
    """Get state manager instance."""
    if _state_manager is None:
        raise RuntimeError("State manager not initialized")
    return _state_manager


def get_jwt_handler() -> JWTHandler:
    """Get JWT handler instance."""
    if _jwt_handler is None:
        raise RuntimeError("JWT handler not initialized")
    return _jwt_handler


def get_workspace_manager() -> WorkspaceManager:
    """Get workspace manager instance."""
    if _workspace_manager is None:
        raise RuntimeError("Workspace manager not initialized")
    return _workspace_manager


def get_project_manager() -> ProjectManager:
    """Get project manager instance."""
    if _project_manager is None:
        raise RuntimeError("Project manager not initialized")
    return _project_manager


def get_lock_manager() -> LockManager:
    """Get lock manager instance."""
    if _lock_manager is None:
        raise RuntimeError("Lock manager not initialized")
    return _lock_manager


# =============================================================================
# Authentication Endpoints
# =============================================================================


@router.post("/auth/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register_user(request: UserCreate) -> UserResponse:
    """Register a new user.

    Args:
        request: User registration request.

    Returns:
        Created user.
    """
    state = get_state_manager()
    jwt = get_jwt_handler()

    # Check if email already exists
    existing = await state.get_user_by_email(request.email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    user_id = str(uuid.uuid4())
    password_hash = jwt.hash_password(request.password)

    user = await state.create_user(
        user_id=user_id,
        email=request.email,
        name=request.name,
        password_hash=password_hash,
    )

    logger.info("user_registered", user_id=user_id, email=request.email)
    return user


@router.post("/auth/login", response_model=TokenResponse)
async def login(request: UserLogin) -> TokenResponse:
    """Login and get access token.

    Args:
        request: Login credentials.

    Returns:
        Access token response.
    """
    state = get_state_manager()
    jwt = get_jwt_handler()

    result = await state.get_user_by_email(request.email)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    user, password_hash = result
    if not jwt.verify_password(request.password, password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    # Update last login
    await state.update_user_last_login(user.id)

    # Create tokens
    access_token = jwt.create_access_token(
        user_id=user.id,
        email=user.email,
        role=user.role.value,
    )

    logger.info("user_login", user_id=user.id)

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=jwt.get_token_expiry_seconds(),
    )


@router.get("/auth/me", response_model=UserResponse)
async def get_current_user_info(
    user: CurrentUser,
) -> UserResponse:
    """Get current user info.

    Args:
        user: Current authenticated user.

    Returns:
        User details.
    """
    state = get_state_manager()
    user_data = await state.get_user(user["sub"])

    if user_data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    return user_data


# =============================================================================
# Workspace Endpoints
# =============================================================================


@router.post("/workspaces", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    request: WorkspaceCreate,
    user: CurrentUser,
) -> WorkspaceResponse:
    """Create a new workspace.

    Args:
        request: Workspace creation request.
        user: Current authenticated user.

    Returns:
        Created workspace.
    """
    workspace_mgr = get_workspace_manager()

    workspace = await workspace_mgr.create_workspace(
        name=request.name,
        owner_id=user["sub"],
        settings=request.settings,
    )

    return workspace


@router.get("/workspaces", response_model=list[WorkspaceResponse])
async def list_workspaces(user: CurrentUser) -> list[WorkspaceResponse]:
    """List workspaces the user belongs to.

    Args:
        user: Current authenticated user.

    Returns:
        List of workspaces.
    """
    workspace_mgr = get_workspace_manager()
    return await workspace_mgr.get_user_workspaces(user["sub"])


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: str,
    user: CurrentUser,
) -> WorkspaceResponse:
    """Get workspace details.

    Args:
        workspace_id: Workspace ID.
        user: Current authenticated user.

    Returns:
        Workspace details.
    """
    workspace_mgr = get_workspace_manager()

    try:
        return await workspace_mgr.get_workspace(workspace_id, user["sub"])
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


@router.put("/workspaces/{workspace_id}", response_model=WorkspaceResponse)
async def update_workspace(
    workspace_id: str,
    request: WorkspaceUpdate,
    user: CurrentUser,
) -> WorkspaceResponse:
    """Update a workspace.

    Args:
        workspace_id: Workspace ID.
        request: Update request.
        user: Current authenticated user.

    Returns:
        Updated workspace.
    """
    workspace_mgr = get_workspace_manager()

    try:
        return await workspace_mgr.update_workspace(
            workspace_id=workspace_id,
            user_id=user["sub"],
            name=request.name,
            settings=request.settings,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )


@router.delete("/workspaces/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: str,
    user: CurrentUser,
) -> None:
    """Delete a workspace.

    Args:
        workspace_id: Workspace ID.
        user: Current authenticated user.
    """
    workspace_mgr = get_workspace_manager()

    try:
        await workspace_mgr.delete_workspace(workspace_id, user["sub"])
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )


@router.post("/workspaces/{workspace_id}/members", status_code=status.HTTP_201_CREATED)
async def add_workspace_member(
    workspace_id: str,
    request: WorkspaceMemberAdd,
    user: CurrentUser,
) -> dict[str, str]:
    """Add a member to a workspace.

    Args:
        workspace_id: Workspace ID.
        request: Member add request.
        user: Current authenticated user.

    Returns:
        Success message.
    """
    workspace_mgr = get_workspace_manager()

    try:
        await workspace_mgr.add_member(
            workspace_id=workspace_id,
            user_id=user["sub"],
            new_member_id=request.user_id,
            role=request.role,
        )
        return {"message": "Member added successfully"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )


@router.delete("/workspaces/{workspace_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_workspace_member(
    workspace_id: str,
    member_id: str,
    user: CurrentUser,
) -> None:
    """Remove a member from a workspace.

    Args:
        workspace_id: Workspace ID.
        member_id: User ID to remove.
        user: Current authenticated user.
    """
    workspace_mgr = get_workspace_manager()

    try:
        await workspace_mgr.remove_member(workspace_id, user["sub"], member_id)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )


@router.get("/workspaces/{workspace_id}/members", response_model=list[WorkspaceMemberResponse])
async def list_workspace_members(
    workspace_id: str,
    user: CurrentUser,
) -> list[WorkspaceMemberResponse]:
    """List workspace members.

    Args:
        workspace_id: Workspace ID.
        user: Current authenticated user.

    Returns:
        List of members.
    """
    workspace_mgr = get_workspace_manager()

    try:
        return await workspace_mgr.get_members(workspace_id, user["sub"])
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )


# =============================================================================
# Project Endpoints
# =============================================================================


@router.post(
    "/workspaces/{workspace_id}/projects",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_project(
    workspace_id: str,
    request: ProjectCreate,
    user: CurrentUser,
) -> ProjectResponse:
    """Create a project in a workspace.

    Args:
        workspace_id: Workspace ID.
        request: Project creation request.
        user: Current authenticated user.

    Returns:
        Created project.
    """
    project_mgr = get_project_manager()

    try:
        return await project_mgr.create_project(
            workspace_id=workspace_id,
            name=request.name,
            user_id=user["sub"],
            description=request.description,
            settings=request.settings,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )


@router.get("/workspaces/{workspace_id}/projects", response_model=list[ProjectResponse])
async def list_projects(
    workspace_id: str,
    user: CurrentUser,
) -> list[ProjectResponse]:
    """List projects in a workspace.

    Args:
        workspace_id: Workspace ID.
        user: Current authenticated user.

    Returns:
        List of projects.
    """
    project_mgr = get_project_manager()

    try:
        return await project_mgr.get_workspace_projects(workspace_id, user["sub"])
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: str,
    user: CurrentUser,
) -> ProjectResponse:
    """Get project details.

    Args:
        project_id: Project ID.
        user: Current authenticated user.

    Returns:
        Project details.
    """
    project_mgr = get_project_manager()

    try:
        return await project_mgr.get_project(project_id, user["sub"])
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


@router.put("/projects/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: str,
    request: ProjectUpdate,
    user: CurrentUser,
) -> ProjectResponse:
    """Update a project.

    Args:
        project_id: Project ID.
        request: Update request.
        user: Current authenticated user.

    Returns:
        Updated project.
    """
    project_mgr = get_project_manager()

    try:
        return await project_mgr.update_project(
            project_id=project_id,
            user_id=user["sub"],
            name=request.name,
            description=request.description,
            status=request.status,
            settings=request.settings,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: str,
    user: CurrentUser,
) -> None:
    """Delete a project.

    Args:
        project_id: Project ID.
        user: Current authenticated user.
    """
    project_mgr = get_project_manager()

    try:
        await project_mgr.delete_project(project_id, user["sub"])
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )


@router.get("/projects/{project_id}/tasks", response_model=list[TaskResponse])
async def list_project_tasks(
    project_id: str,
    user: CurrentUser,
    status_filter: TaskStatus | None = None,
) -> list[TaskResponse]:
    """List tasks in a project.

    Args:
        project_id: Project ID.
        user: Current authenticated user.
        status_filter: Optional status filter.

    Returns:
        List of tasks.
    """
    project_mgr = get_project_manager()

    try:
        return await project_mgr.get_project_tasks(project_id, user["sub"], status_filter)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )


@router.get("/projects/{project_id}/stats")
async def get_project_stats(
    project_id: str,
    user: CurrentUser,
) -> dict[str, Any]:
    """Get project statistics.

    Args:
        project_id: Project ID.
        user: Current authenticated user.

    Returns:
        Project statistics.
    """
    project_mgr = get_project_manager()

    try:
        return await project_mgr.get_project_stats(project_id, user["sub"])
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )


# =============================================================================
# Lock Endpoints
# =============================================================================


@router.post("/locks", response_model=LockResponse, status_code=status.HTTP_201_CREATED)
async def acquire_lock(
    request: LockAcquire,
    user: CurrentUser,
) -> LockResponse:
    """Acquire a lock on a resource.

    Args:
        request: Lock acquisition request.
        user: Current authenticated user.

    Returns:
        Lock details.
    """
    lock_mgr = get_lock_manager()

    try:
        lock = await lock_mgr.acquire(
            resource_type=request.resource_type,
            resource_id=request.resource_id,
            holder_id=user["sub"],
            ttl_seconds=request.ttl_seconds,
        )
        return lock
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )


@router.delete("/locks/{lock_id}", status_code=status.HTTP_204_NO_CONTENT)
async def release_lock(
    lock_id: str,
    user: CurrentUser,
) -> None:
    """Release a lock.

    Args:
        lock_id: Lock ID.
        user: Current authenticated user.
    """
    lock_mgr = get_lock_manager()

    try:
        await lock_mgr.release(lock_id, user["sub"])
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )


@router.get("/locks", response_model=list[LockResponse])
async def list_locks(
    user: CurrentUser,
    holder_id: str | None = None,
) -> list[LockResponse]:
    """List active locks.

    Args:
        user: Current authenticated user.
        holder_id: Optional filter by holder.

    Returns:
        List of locks.
    """
    lock_mgr = get_lock_manager()

    # Non-admin can only see their own locks
    if user.get("role") != UserRole.ADMIN.value and holder_id != user["sub"]:
        holder_id = user["sub"]

    return await lock_mgr.get_holder_locks(holder_id) if holder_id else await lock_mgr.get_all_locks()


# =============================================================================
# Webhook Endpoints
# =============================================================================


@router.post(
    "/workspaces/{workspace_id}/webhooks",
    response_model=WebhookResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_webhook(
    workspace_id: str,
    request: WebhookCreate,
    user: CurrentUser,
) -> WebhookResponse:
    """Create a webhook for a workspace.

    Args:
        workspace_id: Workspace ID.
        request: Webhook creation request.
        user: Current authenticated user.

    Returns:
        Created webhook.
    """
    state = get_state_manager()
    workspace_mgr = get_workspace_manager()

    # Verify workspace access
    try:
        await workspace_mgr.get_workspace(workspace_id, user["sub"])
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access to workspace",
        )

    webhook_id = str(uuid.uuid4())
    return await state.create_webhook(
        webhook_id=webhook_id,
        workspace_id=workspace_id,
        service=request.service,
        url=request.url,
        events=request.events,
    )


@router.get("/workspaces/{workspace_id}/webhooks", response_model=list[WebhookResponse])
async def list_webhooks(
    workspace_id: str,
    user: CurrentUser,
) -> list[WebhookResponse]:
    """List webhooks for a workspace.

    Args:
        workspace_id: Workspace ID.
        user: Current authenticated user.

    Returns:
        List of webhooks.
    """
    state = get_state_manager()
    workspace_mgr = get_workspace_manager()

    # Verify workspace access
    try:
        await workspace_mgr.get_workspace(workspace_id, user["sub"])
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access to workspace",
        )

    return await state.get_webhooks_for_workspace(workspace_id)


@router.put("/webhooks/{webhook_id}", response_model=WebhookResponse)
async def update_webhook(
    webhook_id: str,
    request: WebhookUpdate,
    user: CurrentUser,
) -> WebhookResponse:
    """Update a webhook.

    Args:
        webhook_id: Webhook ID.
        request: Update request.
        user: Current authenticated user.

    Returns:
        Updated webhook.
    """
    state = get_state_manager()
    workspace_mgr = get_workspace_manager()

    webhook = await state.get_webhook(webhook_id)
    if webhook is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook not found",
        )

    # Verify workspace access
    try:
        await workspace_mgr.get_workspace(webhook.workspace_id, user["sub"])
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access to workspace",
        )

    updated = await state.update_webhook(
        webhook_id=webhook_id,
        url=request.url,
        events=request.events,
        active=request.active,
    )

    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook not found",
        )

    return updated


@router.delete("/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    webhook_id: str,
    user: CurrentUser,
) -> None:
    """Delete a webhook.

    Args:
        webhook_id: Webhook ID.
        user: Current authenticated user.
    """
    state = get_state_manager()
    workspace_mgr = get_workspace_manager()

    webhook = await state.get_webhook(webhook_id)
    if webhook is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook not found",
        )

    # Verify workspace access
    try:
        await workspace_mgr.get_workspace(webhook.workspace_id, user["sub"])
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access to workspace",
        )

    await state.delete_webhook(webhook_id)


# =============================================================================
# Dashboard Endpoints
# =============================================================================


@router.get("/dashboard/stats", response_model=DashboardStats)
async def get_dashboard_stats(
    user: CurrentUser,
) -> DashboardStats:
    """Get dashboard statistics.

    Args:
        user: Current authenticated user.

    Returns:
        Dashboard statistics.
    """
    state = get_state_manager()

    stats = await state.get_dashboard_stats()
    recent_events = await state.get_recent_events(limit=10)

    return DashboardStats(
        total_workspaces=stats["total_workspaces"],
        total_projects=stats["total_projects"],
        total_users=stats["total_users"],
        total_tasks=stats["total_tasks"],
        tasks_by_status=stats["tasks_by_status"],
        tasks_by_service=stats["tasks_by_service"],
        recent_events=recent_events,
        active_locks=stats["active_locks"],
    )


@router.get("/tasks/{task_id}/events", response_model=list[TaskEventResponse])
async def get_task_events(
    task_id: str,
    user: CurrentUser,
    limit: int = 100,
) -> list[TaskEventResponse]:
    """Get events for a task.

    Args:
        task_id: Task ID.
        user: Current authenticated user.
        limit: Maximum events to return.

    Returns:
        List of task events.
    """
    state = get_state_manager()
    return await state.get_task_events(task_id, limit)
