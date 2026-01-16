"""Shared pytest fixtures for Magickit tests."""

import asyncio
import os
import tempfile
from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio

from magickit.core.state_manager import StateManager
from magickit.core.task_queue import TaskQueue


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create an event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def temp_db_path() -> AsyncGenerator[str, None]:
    """Create a temporary database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    yield db_path

    # Cleanup
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest_asyncio.fixture
async def state_manager(temp_db_path: str) -> AsyncGenerator[StateManager, None]:
    """Create an initialized StateManager."""
    manager = StateManager(db_path=temp_db_path)
    await manager.initialize()

    yield manager

    await manager.close()


@pytest_asyncio.fixture
async def task_queue(state_manager: StateManager) -> AsyncGenerator[TaskQueue, None]:
    """Create an initialized TaskQueue."""
    queue = TaskQueue(
        state_manager=state_manager,
        max_concurrent=5,
        default_priority=5,
        max_retries=3,
    )
    await queue.initialize()

    yield queue
