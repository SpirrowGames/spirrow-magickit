"""The dashboard fragments build HTML with f-strings, so they must escape.

These panels render names and ids that came from somewhere else -- a task
name arrives through the MCP tools, a lock's resource_type is a free-form
string on the lock request -- and they go straight into markup. The
dashboard is the one place a human reads them.

The handlers are closures inside `create_app`, and reaching them over HTTP
would need the app's lifespan, which opens the real SQLite file the
running service holds a lock on. They are called directly instead, with a
stub request carrying a fake state manager: the escaping is a property of
the handler, not of the transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import SimpleNamespace

import pytest

from magickit.main import create_app
from tests.route_table import sole_route

# The name a careless f-string turns into markup rather than text.
PAYLOAD = '<script>alert("xss")</script>'
ESCAPED = "&lt;script&gt;"


class _Value(str, Enum):
    LEXORA = "lexora"


class _Status(str, Enum):
    PENDING = "pending"


@dataclass
class _Task:
    id: str = "t-1"
    name: str = PAYLOAD
    description: str = PAYLOAD
    service: _Value = _Value.LEXORA
    priority: int = 5
    status: _Status = _Status.PENDING
    created_at: datetime = field(default_factory=lambda: datetime(2026, 1, 1))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    retry_count: int = 0
    dependencies: list[str] = field(default_factory=lambda: [PAYLOAD])
    error: str | None = PAYLOAD
    result: dict | None = field(default_factory=lambda: {"out": PAYLOAD})


@dataclass
class _Lock:
    resource_type: str = PAYLOAD
    resource_id: str = PAYLOAD
    holder_id: str = PAYLOAD


@dataclass
class _Project:
    name: str = PAYLOAD
    description: str = PAYLOAD
    status: _Status = _Status.PENDING
    created_at: datetime = field(default_factory=lambda: datetime(2026, 1, 1))


def _request(**state):
    """A stub with just the attributes the fragment handlers reach for."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


class _StateManager:
    def __init__(self, **returns):
        self._returns = returns

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            return self._returns.get(name)

        return call


async def _render(app, method, path, request, **kwargs):
    response = await sole_route(app, method, path).endpoint(request, **kwargs)
    return response.body.decode()


@pytest.mark.asyncio
async def test_queue_fragment_escapes_task_names():
    app = create_app()
    queue = SimpleNamespace(get_all_tasks=lambda: _async([_Task()]))
    body = await _render(
        app, "GET", "/dashboard/queue", _request(task_queue=queue)
    )

    assert PAYLOAD not in body
    assert ESCAPED in body


@pytest.mark.asyncio
async def test_locks_fragment_escapes_resource_strings():
    app = create_app()
    request = _request(state_manager=_StateManager(get_active_locks=[_Lock()]))
    body = await _render(app, "GET", "/dashboard/locks", request)

    assert PAYLOAD not in body
    assert ESCAPED in body


@pytest.mark.asyncio
async def test_projects_fragment_escapes_names_and_descriptions():
    app = create_app()
    request = _request(
        state_manager=_StateManager(get_projects_in_workspace=[_Project()])
    )
    body = await _render(app, "GET", "/dashboard/_projects", request)

    assert PAYLOAD not in body
    assert ESCAPED in body


@pytest.mark.asyncio
async def test_tasks_fragment_escapes_names():
    app = create_app()
    request = _request(state_manager=_StateManager(get_all_tasks=[_Task()]))
    body = await _render(app, "GET", "/dashboard/_tasks", request)

    assert PAYLOAD not in body
    assert ESCAPED in body


@pytest.mark.asyncio
async def test_task_detail_escapes_every_field_it_shows():
    """The detail modal renders the widest set of strings, including the
    error text and the result dict -- the two most likely to hold something
    that was never meant to be markup."""
    app = create_app()
    request = _request(state_manager=_StateManager(get_task=_Task()))
    body = await _render(
        app, "GET", "/dashboard/_tasks/{task_id}", request, task_id="t-1"
    )

    assert PAYLOAD not in body
    assert ESCAPED in body
    # The fields that only appear when set must be covered too, not just
    # the ones every task has.
    assert "Error" in body and "Result" in body and "Depends on" in body


async def _async(value):
    return value
