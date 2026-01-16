"""Unit tests for lock manager."""

import pytest
import pytest_asyncio

from magickit.core.lock_manager import (
    LockAcquisitionError,
    LockManager,
    LockNotHeldError,
)
from magickit.core.state_manager import StateManager


@pytest_asyncio.fixture
async def state_manager(tmp_path):
    """Create a state manager with temporary database."""
    db_path = str(tmp_path / "test.db")
    manager = StateManager(db_path=db_path)
    await manager.initialize()

    # Run Phase 2 migrations
    from magickit.core.migrations import MigrationManager
    migration_manager = MigrationManager(db_path=db_path)
    await migration_manager.migrate()

    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def lock_manager(state_manager):
    """Create a lock manager."""
    return LockManager(state_manager)


@pytest.mark.asyncio
async def test_acquire_lock(lock_manager):
    """Test acquiring a lock."""
    lock = await lock_manager.acquire(
        resource_type="task",
        resource_id="task-1",
        holder_id="user-1",
    )

    assert lock.resource_type == "task"
    assert lock.resource_id == "task-1"
    assert lock.holder_id == "user-1"
    assert lock.id is not None


@pytest.mark.asyncio
async def test_acquire_lock_with_ttl(lock_manager):
    """Test acquiring a lock with custom TTL."""
    lock = await lock_manager.acquire(
        resource_type="project",
        resource_id="project-1",
        holder_id="user-1",
        ttl_seconds=60,
    )

    assert lock.expires_at is not None


@pytest.mark.asyncio
async def test_acquire_lock_already_locked(lock_manager):
    """Test acquiring a lock that's already held."""
    # First lock succeeds
    await lock_manager.acquire(
        resource_type="task",
        resource_id="task-1",
        holder_id="user-1",
    )

    # Second lock should fail
    with pytest.raises(LockAcquisitionError):
        await lock_manager.acquire(
            resource_type="task",
            resource_id="task-1",
            holder_id="user-2",
        )


@pytest.mark.asyncio
async def test_release_lock(lock_manager):
    """Test releasing a lock."""
    lock = await lock_manager.acquire(
        resource_type="task",
        resource_id="task-1",
        holder_id="user-1",
    )

    result = await lock_manager.release(lock.id, "user-1")
    assert result is True

    # Resource should now be unlocked
    is_locked = await lock_manager.is_locked("task", "task-1")
    assert is_locked is False


@pytest.mark.asyncio
async def test_release_lock_wrong_holder(lock_manager):
    """Test that only the holder can release a lock."""
    lock = await lock_manager.acquire(
        resource_type="task",
        resource_id="task-1",
        holder_id="user-1",
    )

    with pytest.raises(LockNotHeldError):
        await lock_manager.release(lock.id, "user-2")


@pytest.mark.asyncio
async def test_get_lock(lock_manager):
    """Test getting a lock by resource."""
    await lock_manager.acquire(
        resource_type="project",
        resource_id="project-1",
        holder_id="user-1",
    )

    lock = await lock_manager.get_lock("project", "project-1")
    assert lock is not None
    assert lock.holder_id == "user-1"


@pytest.mark.asyncio
async def test_get_lock_not_exists(lock_manager):
    """Test getting a lock that doesn't exist."""
    lock = await lock_manager.get_lock("task", "non-existent")
    assert lock is None


@pytest.mark.asyncio
async def test_is_locked(lock_manager):
    """Test checking if a resource is locked."""
    # Should not be locked initially
    is_locked = await lock_manager.is_locked("task", "task-1")
    assert is_locked is False

    # Lock it
    await lock_manager.acquire(
        resource_type="task",
        resource_id="task-1",
        holder_id="user-1",
    )

    # Should be locked now
    is_locked = await lock_manager.is_locked("task", "task-1")
    assert is_locked is True


@pytest.mark.asyncio
async def test_get_holder_locks(lock_manager):
    """Test getting all locks held by a user."""
    # Create multiple locks
    await lock_manager.acquire("task", "task-1", "user-1")
    await lock_manager.acquire("task", "task-2", "user-1")
    await lock_manager.acquire("task", "task-3", "user-2")

    locks = await lock_manager.get_holder_locks("user-1")
    assert len(locks) == 2

    resource_ids = [l.resource_id for l in locks]
    assert "task-1" in resource_ids
    assert "task-2" in resource_ids


@pytest.mark.asyncio
async def test_get_all_locks(lock_manager):
    """Test getting all active locks."""
    await lock_manager.acquire("task", "task-1", "user-1")
    await lock_manager.acquire("project", "project-1", "user-2")

    locks = await lock_manager.get_all_locks()
    assert len(locks) == 2


@pytest.mark.asyncio
async def test_lock_context_manager(lock_manager):
    """Test using lock as context manager."""
    async with lock_manager.hold("task", "task-1", "user-1") as lock:
        assert lock.holder_id == "user-1"
        is_locked = await lock_manager.is_locked("task", "task-1")
        assert is_locked is True

    # Lock should be released after context
    is_locked = await lock_manager.is_locked("task", "task-1")
    assert is_locked is False


@pytest.mark.asyncio
async def test_different_resource_types(lock_manager):
    """Test that different resource types can be locked independently."""
    # Same resource ID but different types
    lock1 = await lock_manager.acquire("task", "id-1", "user-1")
    lock2 = await lock_manager.acquire("project", "id-1", "user-1")

    assert lock1.id != lock2.id
    assert await lock_manager.is_locked("task", "id-1")
    assert await lock_manager.is_locked("project", "id-1")


@pytest.mark.asyncio
async def test_reacquire_after_release(lock_manager):
    """Test acquiring a lock after it's been released."""
    # First acquisition
    lock1 = await lock_manager.acquire("task", "task-1", "user-1")
    await lock_manager.release(lock1.id, "user-1")

    # Second acquisition should work
    lock2 = await lock_manager.acquire("task", "task-1", "user-2")
    assert lock2.holder_id == "user-2"


@pytest.mark.asyncio
async def test_same_holder_cannot_double_lock(lock_manager):
    """Test that the same holder cannot acquire the same lock twice."""
    await lock_manager.acquire("task", "task-1", "user-1")

    # Same holder trying to lock again should fail
    with pytest.raises(LockAcquisitionError):
        await lock_manager.acquire("task", "task-1", "user-1")
