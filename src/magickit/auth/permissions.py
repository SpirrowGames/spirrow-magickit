"""Role-based access control (RBAC) permission system."""

from __future__ import annotations

from enum import Enum
from functools import wraps
from typing import Any, Callable

from fastapi import HTTPException, Request, status

from magickit.api.models import UserRole
from magickit.utils.logging import get_logger

logger = get_logger(__name__)


class Permission(str, Enum):
    """Available permissions in the system."""

    # Workspace permissions
    WORKSPACE_CREATE = "workspace:create"
    WORKSPACE_READ = "workspace:read"
    WORKSPACE_UPDATE = "workspace:update"
    WORKSPACE_DELETE = "workspace:delete"
    WORKSPACE_MANAGE_MEMBERS = "workspace:manage_members"

    # Project permissions
    PROJECT_CREATE = "project:create"
    PROJECT_READ = "project:read"
    PROJECT_UPDATE = "project:update"
    PROJECT_DELETE = "project:delete"
    PROJECT_MANAGE_MEMBERS = "project:manage_members"

    # Task permissions
    TASK_CREATE = "task:create"
    TASK_READ = "task:read"
    TASK_UPDATE = "task:update"
    TASK_DELETE = "task:delete"
    TASK_EXECUTE = "task:execute"

    # Lock permissions
    LOCK_CREATE = "lock:create"
    LOCK_READ = "lock:read"
    LOCK_RELEASE = "lock:release"

    # Webhook permissions
    WEBHOOK_CREATE = "webhook:create"
    WEBHOOK_READ = "webhook:read"
    WEBHOOK_UPDATE = "webhook:update"
    WEBHOOK_DELETE = "webhook:delete"

    # Admin permissions
    ADMIN_USERS = "admin:users"
    ADMIN_SYSTEM = "admin:system"


# Role to permissions mapping
ROLE_PERMISSIONS: dict[UserRole, set[Permission]] = {
    UserRole.ADMIN: set(Permission),  # Admins have all permissions
    UserRole.MEMBER: {
        # Workspace
        Permission.WORKSPACE_READ,
        # Project
        Permission.PROJECT_CREATE,
        Permission.PROJECT_READ,
        Permission.PROJECT_UPDATE,
        # Task
        Permission.TASK_CREATE,
        Permission.TASK_READ,
        Permission.TASK_UPDATE,
        Permission.TASK_DELETE,
        Permission.TASK_EXECUTE,
        # Lock
        Permission.LOCK_CREATE,
        Permission.LOCK_READ,
        Permission.LOCK_RELEASE,
        # Webhook
        Permission.WEBHOOK_READ,
    },
    UserRole.VIEWER: {
        Permission.WORKSPACE_READ,
        Permission.PROJECT_READ,
        Permission.TASK_READ,
        Permission.LOCK_READ,
        Permission.WEBHOOK_READ,
    },
}


def get_permissions_for_role(role: UserRole) -> set[Permission]:
    """Get all permissions for a role.

    Args:
        role: User role.

    Returns:
        Set of permissions.
    """
    return ROLE_PERMISSIONS.get(role, set())


def has_permission(user_role: UserRole, permission: Permission) -> bool:
    """Check if a role has a specific permission.

    Args:
        user_role: User's role.
        permission: Permission to check.

    Returns:
        True if role has permission, False otherwise.
    """
    role_permissions = get_permissions_for_role(user_role)
    return permission in role_permissions


def require_permission(
    permission: Permission,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to require a specific permission for an endpoint.

    Args:
        permission: Required permission.

    Returns:
        Decorator function.

    Example:
        @router.get("/admin/users")
        @require_permission(Permission.ADMIN_USERS)
        async def list_users(request: Request, user: CurrentUser):
            ...
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Find the request and user in kwargs
            request: Request | None = kwargs.get("request")
            user: dict[str, Any] | None = kwargs.get("user")

            if not user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required",
                )

            user_role = UserRole(user.get("role", UserRole.VIEWER.value))

            if not has_permission(user_role, permission):
                logger.warning(
                    "permission_denied",
                    user_id=user.get("sub"),
                    role=user_role.value,
                    required_permission=permission.value,
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission denied: {permission.value} required",
                )

            return await func(*args, **kwargs)

        return wrapper

    return decorator


class PermissionChecker:
    """Helper class for checking permissions in endpoints."""

    def __init__(self, user: dict[str, Any]) -> None:
        """Initialize permission checker.

        Args:
            user: User payload from JWT.
        """
        self.user_id = user.get("sub", "")
        self.role = UserRole(user.get("role", UserRole.VIEWER.value))
        self.permissions = get_permissions_for_role(self.role)

    def has(self, permission: Permission) -> bool:
        """Check if user has a permission.

        Args:
            permission: Permission to check.

        Returns:
            True if user has permission.
        """
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        """Require a permission, raise if not present.

        Args:
            permission: Permission to require.

        Raises:
            HTTPException: If permission not present.
        """
        if not self.has(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {permission.value} required",
            )

    def is_admin(self) -> bool:
        """Check if user is admin.

        Returns:
            True if admin.
        """
        return self.role == UserRole.ADMIN
