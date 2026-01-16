"""FastAPI dependency injection for authentication."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from magickit.api.models import UserRole
from magickit.utils.logging import get_logger

if TYPE_CHECKING:
    from magickit.api.models import UserResponse

logger = get_logger(__name__)

# Optional bearer token scheme (doesn't raise on missing token)
optional_bearer = HTTPBearer(auto_error=False)
# Required bearer token scheme
required_bearer = HTTPBearer(auto_error=True)


async def get_current_user(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(required_bearer)
    ] = None,
) -> dict[str, Any]:
    """Get the current authenticated user.

    Requires valid authentication.

    Args:
        request: FastAPI request.
        credentials: Bearer token credentials.

    Returns:
        User payload from JWT.

    Raises:
        HTTPException: If not authenticated.
    """
    # Check if auth is disabled (development mode)
    if hasattr(request.app.state, "auth_enabled") and not request.app.state.auth_enabled:
        # Return a default user for development
        return {
            "sub": "dev-user",
            "email": "dev@example.com",
            "role": UserRole.ADMIN.value,
        }

    # Try to get user from request state (set by middleware)
    if hasattr(request.state, "user") and request.state.user:
        return request.state.user

    # No user found
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_optional_user(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(optional_bearer)
    ] = None,
) -> dict[str, Any] | None:
    """Get the current user if authenticated, None otherwise.

    Does not require authentication.

    Args:
        request: FastAPI request.
        credentials: Optional bearer token credentials.

    Returns:
        User payload from JWT or None.
    """
    # Check if auth is disabled (development mode)
    if hasattr(request.app.state, "auth_enabled") and not request.app.state.auth_enabled:
        return {
            "sub": "dev-user",
            "email": "dev@example.com",
            "role": UserRole.ADMIN.value,
        }

    # Try to get user from request state (set by middleware)
    if hasattr(request.state, "user") and request.state.user:
        return request.state.user

    return None


async def get_current_user_id(
    user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> str:
    """Get the current user's ID.

    Args:
        user: Current user payload.

    Returns:
        User ID.
    """
    return user["sub"]


async def require_admin(
    user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> dict[str, Any]:
    """Require admin role.

    Args:
        user: Current user payload.

    Returns:
        User payload if admin.

    Raises:
        HTTPException: If not admin.
    """
    if user.get("role") != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user


async def require_workspace_access(
    request: Request,
    user: Annotated[dict[str, Any], Depends(get_current_user)],
    workspace_id: str,
) -> dict[str, Any]:
    """Require access to a specific workspace.

    Args:
        request: FastAPI request.
        user: Current user payload.
        workspace_id: Workspace ID to check access for.

    Returns:
        User payload if has access.

    Raises:
        HTTPException: If no access to workspace.
    """
    # Admins have access to all workspaces
    if user.get("role") == UserRole.ADMIN.value:
        return user

    # Check membership via state manager
    state_manager = request.app.state.state_manager
    is_member = await state_manager.is_workspace_member(workspace_id, user["sub"])

    if not is_member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No access to this workspace",
        )

    return user


# Type aliases for cleaner dependency injection
CurrentUser = Annotated[dict[str, Any], Depends(get_current_user)]
OptionalUser = Annotated[dict[str, Any] | None, Depends(get_optional_user)]
CurrentUserId = Annotated[str, Depends(get_current_user_id)]
AdminUser = Annotated[dict[str, Any], Depends(require_admin)]
