"""Workspace management for multi-tenant support."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from magickit.api.models import (
    UserRole,
    WorkspaceMemberResponse,
    WorkspaceResponse,
)
from magickit.utils.logging import get_logger

if TYPE_CHECKING:
    from magickit.core.state_manager import StateManager

logger = get_logger(__name__)


class WorkspaceError(Exception):
    """Base exception for workspace errors."""

    pass


class WorkspaceNotFoundError(WorkspaceError):
    """Raised when workspace is not found."""

    pass


class WorkspaceAccessDeniedError(WorkspaceError):
    """Raised when user doesn't have access to workspace."""

    pass


class WorkspaceManager:
    """Manages workspace operations.

    Provides high-level workspace management with access control.
    """

    def __init__(self, state_manager: StateManager) -> None:
        """Initialize workspace manager.

        Args:
            state_manager: State manager for persistence.
        """
        self._state = state_manager

    async def create_workspace(
        self,
        name: str,
        owner_id: str,
        settings: dict[str, Any] | None = None,
    ) -> WorkspaceResponse:
        """Create a new workspace.

        Creates workspace and adds owner as admin member.

        Args:
            name: Workspace name.
            owner_id: ID of the user creating the workspace.
            settings: Optional workspace settings.

        Returns:
            Created workspace.
        """
        workspace_id = str(uuid.uuid4())

        workspace = await self._state.create_workspace(
            workspace_id=workspace_id,
            name=name,
            owner_id=owner_id,
            settings=settings,
        )

        # Add owner as admin member
        await self._state.add_workspace_member(
            workspace_id=workspace_id,
            user_id=owner_id,
            role=UserRole.ADMIN,
        )

        logger.info(
            "workspace_created",
            workspace_id=workspace_id,
            name=name,
            owner_id=owner_id,
        )

        return workspace

    async def get_workspace(
        self,
        workspace_id: str,
        user_id: str | None = None,
    ) -> WorkspaceResponse:
        """Get a workspace by ID.

        Args:
            workspace_id: Workspace ID.
            user_id: Optional user ID to check access.

        Returns:
            Workspace response.

        Raises:
            WorkspaceNotFoundError: If workspace not found.
            WorkspaceAccessDeniedError: If user doesn't have access.
        """
        workspace = await self._state.get_workspace(workspace_id)

        if workspace is None:
            raise WorkspaceNotFoundError(f"Workspace {workspace_id} not found")

        # Check access if user_id provided
        if user_id:
            has_access = await self._state.is_workspace_member(workspace_id, user_id)
            if not has_access:
                raise WorkspaceAccessDeniedError(
                    f"User {user_id} does not have access to workspace {workspace_id}"
                )

        return workspace

    async def get_user_workspaces(self, user_id: str) -> list[WorkspaceResponse]:
        """Get all workspaces a user is a member of.

        Args:
            user_id: User ID.

        Returns:
            List of workspaces.
        """
        return await self._state.get_workspaces_for_user(user_id)

    async def update_workspace(
        self,
        workspace_id: str,
        user_id: str,
        name: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> WorkspaceResponse:
        """Update a workspace.

        Args:
            workspace_id: Workspace ID.
            user_id: User ID (must be admin or owner).
            name: New name.
            settings: New settings.

        Returns:
            Updated workspace.

        Raises:
            WorkspaceNotFoundError: If workspace not found.
            WorkspaceAccessDeniedError: If user can't modify workspace.
        """
        # Check access and admin role
        await self._require_workspace_admin(workspace_id, user_id)

        workspace = await self._state.update_workspace(
            workspace_id=workspace_id,
            name=name,
            settings=settings,
        )

        if workspace is None:
            raise WorkspaceNotFoundError(f"Workspace {workspace_id} not found")

        logger.info(
            "workspace_updated",
            workspace_id=workspace_id,
            updated_by=user_id,
        )

        return workspace

    async def delete_workspace(self, workspace_id: str, user_id: str) -> bool:
        """Delete a workspace.

        Args:
            workspace_id: Workspace ID.
            user_id: User ID (must be owner).

        Returns:
            True if deleted.

        Raises:
            WorkspaceNotFoundError: If workspace not found.
            WorkspaceAccessDeniedError: If user is not the owner.
        """
        workspace = await self._state.get_workspace(workspace_id)

        if workspace is None:
            raise WorkspaceNotFoundError(f"Workspace {workspace_id} not found")

        # Only owner can delete
        if workspace.owner_id != user_id:
            raise WorkspaceAccessDeniedError(
                "Only the workspace owner can delete it"
            )

        # Prevent deleting default workspace
        if workspace_id == "default":
            raise WorkspaceError("Cannot delete the default workspace")

        result = await self._state.delete_workspace(workspace_id)

        if result:
            logger.info(
                "workspace_deleted",
                workspace_id=workspace_id,
                deleted_by=user_id,
            )

        return result

    async def add_member(
        self,
        workspace_id: str,
        user_id: str,
        new_member_id: str,
        role: UserRole = UserRole.MEMBER,
    ) -> None:
        """Add a member to a workspace.

        Args:
            workspace_id: Workspace ID.
            user_id: User ID performing the action (must be admin).
            new_member_id: User ID to add.
            role: Role for the new member.

        Raises:
            WorkspaceAccessDeniedError: If user can't add members.
        """
        await self._require_workspace_admin(workspace_id, user_id)

        await self._state.add_workspace_member(
            workspace_id=workspace_id,
            user_id=new_member_id,
            role=role,
        )

        logger.info(
            "workspace_member_added",
            workspace_id=workspace_id,
            member_id=new_member_id,
            role=role.value,
            added_by=user_id,
        )

    async def remove_member(
        self,
        workspace_id: str,
        user_id: str,
        member_to_remove: str,
    ) -> bool:
        """Remove a member from a workspace.

        Args:
            workspace_id: Workspace ID.
            user_id: User ID performing the action (must be admin).
            member_to_remove: User ID to remove.

        Returns:
            True if removed.

        Raises:
            WorkspaceAccessDeniedError: If user can't remove members.
            WorkspaceError: If trying to remove the owner.
        """
        workspace = await self.get_workspace(workspace_id, user_id)

        # Check if user is admin
        await self._require_workspace_admin(workspace_id, user_id)

        # Can't remove the owner
        if workspace.owner_id == member_to_remove:
            raise WorkspaceError("Cannot remove the workspace owner")

        result = await self._state.remove_workspace_member(
            workspace_id, member_to_remove
        )

        if result:
            logger.info(
                "workspace_member_removed",
                workspace_id=workspace_id,
                member_id=member_to_remove,
                removed_by=user_id,
            )

        return result

    async def get_members(
        self, workspace_id: str, user_id: str
    ) -> list[WorkspaceMemberResponse]:
        """Get all members of a workspace.

        Args:
            workspace_id: Workspace ID.
            user_id: User ID (must be a member).

        Returns:
            List of workspace members.

        Raises:
            WorkspaceAccessDeniedError: If user doesn't have access.
        """
        # Verify access
        await self.get_workspace(workspace_id, user_id)

        return await self._state.get_workspace_members(workspace_id)

    async def get_member_role(
        self, workspace_id: str, user_id: str
    ) -> UserRole | None:
        """Get a user's role in a workspace.

        Args:
            workspace_id: Workspace ID.
            user_id: User ID.

        Returns:
            User's role or None if not a member.
        """
        members = await self._state.get_workspace_members(workspace_id)

        for member in members:
            if member.user_id == user_id:
                return member.role

        return None

    async def _require_workspace_admin(
        self, workspace_id: str, user_id: str
    ) -> None:
        """Require user to be workspace admin.

        Args:
            workspace_id: Workspace ID.
            user_id: User ID to check.

        Raises:
            WorkspaceAccessDeniedError: If not admin.
        """
        role = await self.get_member_role(workspace_id, user_id)

        if role != UserRole.ADMIN:
            raise WorkspaceAccessDeniedError(
                "Admin access required for this operation"
            )
