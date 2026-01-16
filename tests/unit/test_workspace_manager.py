"""Unit tests for workspace manager."""

import pytest
import pytest_asyncio

from magickit.api.models import UserRole
from magickit.core.state_manager import StateManager
from magickit.core.workspace_manager import (
    WorkspaceAccessDeniedError,
    WorkspaceError,
    WorkspaceManager,
    WorkspaceNotFoundError,
)


@pytest_asyncio.fixture
async def state_manager(tmp_path):
    """Create a state manager with temporary database."""
    db_path = str(tmp_path / "test.db")
    manager = StateManager(db_path=db_path)
    await manager.initialize()

    # Run Phase 2 migrations manually
    from magickit.core.migrations import MigrationManager
    migration_manager = MigrationManager(db_path=db_path)
    await migration_manager.migrate()

    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def workspace_manager(state_manager):
    """Create a workspace manager."""
    return WorkspaceManager(state_manager)


@pytest_asyncio.fixture
async def test_user(state_manager):
    """Create a test user."""
    user = await state_manager.create_user(
        user_id="user-1",
        email="test@example.com",
        name="Test User",
        password_hash="hashed",
    )
    return user


@pytest.mark.asyncio
async def test_create_workspace(workspace_manager, test_user):
    """Test creating a workspace."""
    workspace = await workspace_manager.create_workspace(
        name="Test Workspace",
        owner_id=test_user.id,
    )

    assert workspace.name == "Test Workspace"
    assert workspace.owner_id == test_user.id
    assert workspace.id is not None


@pytest.mark.asyncio
async def test_create_workspace_with_settings(workspace_manager, test_user):
    """Test creating a workspace with settings."""
    settings = {"theme": "dark", "notifications": True}
    workspace = await workspace_manager.create_workspace(
        name="Configured Workspace",
        owner_id=test_user.id,
        settings=settings,
    )

    assert workspace.settings == settings


@pytest.mark.asyncio
async def test_get_workspace(workspace_manager, test_user):
    """Test getting a workspace by ID."""
    workspace = await workspace_manager.create_workspace(
        name="Test Workspace",
        owner_id=test_user.id,
    )

    retrieved = await workspace_manager.get_workspace(workspace.id, test_user.id)
    assert retrieved.id == workspace.id
    assert retrieved.name == workspace.name


@pytest.mark.asyncio
async def test_get_workspace_not_found(workspace_manager, test_user):
    """Test getting a non-existent workspace."""
    with pytest.raises(WorkspaceNotFoundError):
        await workspace_manager.get_workspace("non-existent", test_user.id)


@pytest.mark.asyncio
async def test_get_workspace_access_denied(workspace_manager, state_manager, test_user):
    """Test accessing a workspace without membership."""
    # Create workspace with test_user
    workspace = await workspace_manager.create_workspace(
        name="Private Workspace",
        owner_id=test_user.id,
    )

    # Create another user
    other_user = await state_manager.create_user(
        user_id="user-2",
        email="other@example.com",
        name="Other User",
        password_hash="hashed",
    )

    # Other user should not have access
    with pytest.raises(WorkspaceAccessDeniedError):
        await workspace_manager.get_workspace(workspace.id, other_user.id)


@pytest.mark.asyncio
async def test_get_user_workspaces(workspace_manager, test_user):
    """Test getting all workspaces for a user."""
    # Create multiple workspaces
    ws1 = await workspace_manager.create_workspace("Workspace 1", test_user.id)
    ws2 = await workspace_manager.create_workspace("Workspace 2", test_user.id)

    workspaces = await workspace_manager.get_user_workspaces(test_user.id)
    assert len(workspaces) == 2
    workspace_ids = [w.id for w in workspaces]
    assert ws1.id in workspace_ids
    assert ws2.id in workspace_ids


@pytest.mark.asyncio
async def test_update_workspace(workspace_manager, test_user):
    """Test updating a workspace."""
    workspace = await workspace_manager.create_workspace(
        name="Original Name",
        owner_id=test_user.id,
    )

    updated = await workspace_manager.update_workspace(
        workspace_id=workspace.id,
        user_id=test_user.id,
        name="Updated Name",
        settings={"new": "setting"},
    )

    assert updated.name == "Updated Name"
    assert updated.settings == {"new": "setting"}


@pytest.mark.asyncio
async def test_delete_workspace(workspace_manager, test_user):
    """Test deleting a workspace."""
    workspace = await workspace_manager.create_workspace(
        name="To Delete",
        owner_id=test_user.id,
    )

    result = await workspace_manager.delete_workspace(workspace.id, test_user.id)
    assert result is True

    # Should not be found anymore
    with pytest.raises(WorkspaceNotFoundError):
        await workspace_manager.get_workspace(workspace.id)


@pytest.mark.asyncio
async def test_delete_workspace_only_owner(workspace_manager, state_manager, test_user):
    """Test that only owner can delete workspace."""
    workspace = await workspace_manager.create_workspace(
        name="Protected Workspace",
        owner_id=test_user.id,
    )

    # Create another user and add as member
    other_user = await state_manager.create_user(
        user_id="user-3",
        email="member@example.com",
        name="Member User",
        password_hash="hashed",
    )
    await workspace_manager.add_member(workspace.id, test_user.id, other_user.id)

    # Other user (member) cannot delete
    with pytest.raises(WorkspaceAccessDeniedError):
        await workspace_manager.delete_workspace(workspace.id, other_user.id)


@pytest.mark.asyncio
async def test_add_member(workspace_manager, state_manager, test_user):
    """Test adding a member to a workspace."""
    workspace = await workspace_manager.create_workspace(
        name="Team Workspace",
        owner_id=test_user.id,
    )

    # Create new user to add
    new_user = await state_manager.create_user(
        user_id="user-4",
        email="new@example.com",
        name="New User",
        password_hash="hashed",
    )

    await workspace_manager.add_member(
        workspace_id=workspace.id,
        user_id=test_user.id,
        new_member_id=new_user.id,
        role=UserRole.MEMBER,
    )

    # New user should now have access
    retrieved = await workspace_manager.get_workspace(workspace.id, new_user.id)
    assert retrieved.id == workspace.id


@pytest.mark.asyncio
async def test_remove_member(workspace_manager, state_manager, test_user):
    """Test removing a member from a workspace."""
    workspace = await workspace_manager.create_workspace(
        name="Team Workspace",
        owner_id=test_user.id,
    )

    # Add and then remove a member
    member = await state_manager.create_user(
        user_id="user-5",
        email="member@example.com",
        name="Member",
        password_hash="hashed",
    )
    await workspace_manager.add_member(workspace.id, test_user.id, member.id)

    result = await workspace_manager.remove_member(workspace.id, test_user.id, member.id)
    assert result is True

    # Member should no longer have access
    with pytest.raises(WorkspaceAccessDeniedError):
        await workspace_manager.get_workspace(workspace.id, member.id)


@pytest.mark.asyncio
async def test_cannot_remove_owner(workspace_manager, test_user):
    """Test that workspace owner cannot be removed."""
    workspace = await workspace_manager.create_workspace(
        name="Owner Workspace",
        owner_id=test_user.id,
    )

    with pytest.raises(WorkspaceError, match="Cannot remove the workspace owner"):
        await workspace_manager.remove_member(workspace.id, test_user.id, test_user.id)


@pytest.mark.asyncio
async def test_get_members(workspace_manager, state_manager, test_user):
    """Test getting workspace members."""
    workspace = await workspace_manager.create_workspace(
        name="Team Workspace",
        owner_id=test_user.id,
    )

    # Add a member
    member = await state_manager.create_user(
        user_id="user-6",
        email="teammember@example.com",
        name="Team Member",
        password_hash="hashed",
    )
    await workspace_manager.add_member(workspace.id, test_user.id, member.id)

    members = await workspace_manager.get_members(workspace.id, test_user.id)
    assert len(members) == 2  # Owner + 1 member

    member_ids = [m.user_id for m in members]
    assert test_user.id in member_ids
    assert member.id in member_ids


@pytest.mark.asyncio
async def test_get_member_role(workspace_manager, state_manager, test_user):
    """Test getting a member's role."""
    workspace = await workspace_manager.create_workspace(
        name="Role Workspace",
        owner_id=test_user.id,
    )

    # Owner should be admin
    role = await workspace_manager.get_member_role(workspace.id, test_user.id)
    assert role == UserRole.ADMIN

    # Add viewer
    viewer = await state_manager.create_user(
        user_id="user-7",
        email="viewer@example.com",
        name="Viewer",
        password_hash="hashed",
    )
    await workspace_manager.add_member(
        workspace.id, test_user.id, viewer.id, UserRole.VIEWER
    )

    role = await workspace_manager.get_member_role(workspace.id, viewer.id)
    assert role == UserRole.VIEWER
