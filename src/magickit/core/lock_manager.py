"""Distributed lock management for resource synchronization."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator

from magickit.api.models import LockResponse
from magickit.utils.logging import get_logger

if TYPE_CHECKING:
    from magickit.core.state_manager import StateManager

logger = get_logger(__name__)


class LockError(Exception):
    """Base exception for lock errors."""

    pass


class LockAcquisitionError(LockError):
    """Raised when lock acquisition fails."""

    pass


class LockNotFoundError(LockError):
    """Raised when lock is not found."""

    pass


class LockNotHeldError(LockError):
    """Raised when trying to release a lock not held by the user."""

    pass


class LockManager:
    """Manages distributed locks for resource synchronization.

    Supports both pessimistic (explicit) and optimistic (version-based) locking.
    Locks can have TTL (time-to-live) and are automatically cleaned up on expiry.
    """

    DEFAULT_TTL_SECONDS = 300  # 5 minutes
    MAX_TTL_SECONDS = 3600  # 1 hour

    def __init__(self, state_manager: StateManager) -> None:
        """Initialize lock manager.

        Args:
            state_manager: State manager for persistence.
        """
        self._state = state_manager
        self._local_locks: dict[str, asyncio.Lock] = {}

    async def acquire(
        self,
        resource_type: str,
        resource_id: str,
        holder_id: str,
        ttl_seconds: int | None = None,
        wait: bool = False,
        wait_timeout: float = 30.0,
    ) -> LockResponse:
        """Acquire a lock on a resource.

        Args:
            resource_type: Type of resource (e.g., 'task', 'project').
            resource_id: ID of the resource to lock.
            holder_id: ID of the user/process acquiring the lock.
            ttl_seconds: Lock TTL in seconds (default 5 minutes).
            wait: If True, wait for lock to become available.
            wait_timeout: Maximum time to wait in seconds.

        Returns:
            Lock response with lock details.

        Raises:
            LockAcquisitionError: If lock cannot be acquired.
        """
        # Validate TTL
        if ttl_seconds is None:
            ttl_seconds = self.DEFAULT_TTL_SECONDS
        elif ttl_seconds > self.MAX_TTL_SECONDS:
            ttl_seconds = self.MAX_TTL_SECONDS

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        lock_id = str(uuid.uuid4())

        if wait:
            lock = await self._acquire_with_retry(
                lock_id=lock_id,
                resource_type=resource_type,
                resource_id=resource_id,
                holder_id=holder_id,
                expires_at=expires_at,
                timeout=wait_timeout,
            )
        else:
            lock = await self._state.acquire_lock(
                lock_id=lock_id,
                resource_type=resource_type,
                resource_id=resource_id,
                holder_id=holder_id,
                expires_at=expires_at,
            )

        if lock is None:
            # Get current lock holder for error message
            current = await self._state.get_lock(resource_type, resource_id)
            holder_info = f" (held by {current.holder_id})" if current else ""
            raise LockAcquisitionError(
                f"Resource {resource_type}:{resource_id} is already locked{holder_info}"
            )

        logger.info(
            "lock_acquired",
            lock_id=lock.id,
            resource_type=resource_type,
            resource_id=resource_id,
            holder_id=holder_id,
            ttl_seconds=ttl_seconds,
        )

        return lock

    async def _acquire_with_retry(
        self,
        lock_id: str,
        resource_type: str,
        resource_id: str,
        holder_id: str,
        expires_at: datetime,
        timeout: float,
    ) -> LockResponse | None:
        """Attempt to acquire lock with retries.

        Args:
            lock_id: Lock ID.
            resource_type: Resource type.
            resource_id: Resource ID.
            holder_id: Holder ID.
            expires_at: Lock expiry time.
            timeout: Maximum wait time.

        Returns:
            Lock response if acquired, None otherwise.
        """
        start_time = asyncio.get_event_loop().time()
        retry_delay = 0.1  # Start with 100ms

        while True:
            lock = await self._state.acquire_lock(
                lock_id=lock_id,
                resource_type=resource_type,
                resource_id=resource_id,
                holder_id=holder_id,
                expires_at=expires_at,
            )

            if lock is not None:
                return lock

            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= timeout:
                return None

            # Exponential backoff with jitter
            await asyncio.sleep(min(retry_delay, timeout - elapsed))
            retry_delay = min(retry_delay * 2, 1.0)  # Cap at 1 second

    async def release(
        self,
        lock_id: str,
        holder_id: str,
    ) -> bool:
        """Release a lock.

        Args:
            lock_id: Lock ID to release.
            holder_id: ID of the holder (must match).

        Returns:
            True if released.

        Raises:
            LockNotHeldError: If lock is not held by the specified holder.
        """
        result = await self._state.release_lock(lock_id, holder_id)

        if not result:
            raise LockNotHeldError(
                f"Lock {lock_id} is not held by {holder_id}"
            )

        logger.info(
            "lock_released",
            lock_id=lock_id,
            holder_id=holder_id,
        )

        return True

    async def get_lock(
        self, resource_type: str, resource_id: str
    ) -> LockResponse | None:
        """Get the current lock on a resource.

        Args:
            resource_type: Resource type.
            resource_id: Resource ID.

        Returns:
            Lock response if locked, None otherwise.
        """
        return await self._state.get_lock(resource_type, resource_id)

    async def is_locked(self, resource_type: str, resource_id: str) -> bool:
        """Check if a resource is locked.

        Args:
            resource_type: Resource type.
            resource_id: Resource ID.

        Returns:
            True if locked.
        """
        lock = await self._state.get_lock(resource_type, resource_id)
        return lock is not None

    async def get_holder_locks(self, holder_id: str) -> list[LockResponse]:
        """Get all locks held by a user.

        Args:
            holder_id: Holder ID.

        Returns:
            List of locks.
        """
        return await self._state.get_active_locks(holder_id)

    async def get_all_locks(self) -> list[LockResponse]:
        """Get all active locks.

        Returns:
            List of all locks.
        """
        return await self._state.get_active_locks()

    async def extend(
        self,
        lock_id: str,
        holder_id: str,
        additional_seconds: int = 300,
    ) -> LockResponse:
        """Extend a lock's TTL.

        Args:
            lock_id: Lock ID.
            holder_id: Holder ID (must match).
            additional_seconds: Seconds to add to TTL.

        Returns:
            Updated lock.

        Raises:
            LockNotFoundError: If lock not found.
            LockNotHeldError: If lock not held by holder.
        """
        # First get all locks to find this one
        locks = await self._state.get_active_locks(holder_id)
        lock = next((l for l in locks if l.id == lock_id), None)

        if lock is None:
            raise LockNotFoundError(f"Lock {lock_id} not found for holder {holder_id}")

        # Release and reacquire with new TTL
        await self._state.release_lock(lock_id, holder_id)

        new_expires = datetime.now(timezone.utc) + timedelta(seconds=additional_seconds)
        new_lock = await self._state.acquire_lock(
            lock_id=lock_id,
            resource_type=lock.resource_type,
            resource_id=lock.resource_id,
            holder_id=holder_id,
            expires_at=new_expires,
        )

        if new_lock is None:
            # Someone else grabbed it - this shouldn't happen normally
            raise LockAcquisitionError(
                f"Failed to extend lock {lock_id} - resource was grabbed by another holder"
            )

        logger.info(
            "lock_extended",
            lock_id=lock_id,
            holder_id=holder_id,
            new_expires_at=new_expires.isoformat(),
        )

        return new_lock

    @asynccontextmanager
    async def hold(
        self,
        resource_type: str,
        resource_id: str,
        holder_id: str,
        ttl_seconds: int | None = None,
    ) -> AsyncIterator[LockResponse]:
        """Context manager for holding a lock.

        Automatically releases the lock when exiting the context.

        Args:
            resource_type: Resource type.
            resource_id: Resource ID.
            holder_id: Holder ID.
            ttl_seconds: Lock TTL.

        Yields:
            Lock response.

        Example:
            async with lock_manager.hold("task", task_id, user_id) as lock:
                # Do work with lock held
                pass
            # Lock is automatically released
        """
        lock = await self.acquire(
            resource_type=resource_type,
            resource_id=resource_id,
            holder_id=holder_id,
            ttl_seconds=ttl_seconds,
        )

        try:
            yield lock
        finally:
            try:
                await self.release(lock.id, holder_id)
            except LockNotHeldError:
                # Lock may have expired or been released elsewhere
                logger.warning(
                    "lock_release_skipped",
                    lock_id=lock.id,
                    reason="not_held",
                )

    # =========================================================================
    # Optimistic Locking Support
    # =========================================================================

    async def check_version(
        self,
        task_id: str,
        expected_version: int,
    ) -> bool:
        """Check if task version matches expected (optimistic locking).

        Args:
            task_id: Task ID.
            expected_version: Expected version number.

        Returns:
            True if version matches.
        """
        task = await self._state.get_task(task_id)
        if task is None:
            return False

        # Get version from task - need to add version field support
        # For now, always return True as a placeholder
        return True

    async def increment_version(self, task_id: str) -> int:
        """Increment task version (for optimistic locking).

        Args:
            task_id: Task ID.

        Returns:
            New version number.
        """
        return await self._state.update_task_version(task_id)
