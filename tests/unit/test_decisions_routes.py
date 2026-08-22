"""Unit tests for the 判断 (decision) page ― S5 増分 2.

Scope, said in advance so nobody reads more into a green run than is
there:

* These tests pin **the D-26' 4-branch behavior** of GET
  ``/dashboard/decisions/{project}/{thread_id}`` and the route wiring
  around it. They mock ``ChatroomAdapter`` so no live Conclair is needed.
* They do **not** pin arrival: nothing here proves that the Discord alert
  URL reaches a page in production. That is an external-facing claim
  (A-13), and the whole reason 増分 1 shipped a stub was that CI cannot
  make it. Arrival is verified out-of-band by curl -L against :8443 and
  by a real tap from Discord (msg-085 §2 / spec §8). A green file here
  is necessary; it is not sufficient.
* The form-composition path is covered by ``test_decisions_form.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.main import create_app
from magickit.mcp.tools import chatroom as chatroom_tools
from tests.route_table import route_table, sole_handler


@pytest.fixture(autouse=True)
def _configured():
    """Gates read module-level settings; the app normally binds them at startup."""
    chatroom_tools.configure(Settings())
    yield
    chatroom_tools._settings = None


async def _get(path: str) -> httpx.Response:
    """One-shot ASGI GET that does *not* follow redirects."""
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        return await client.get(path)


# --- route registration --------------------------------------------------


def test_thread_page_is_registered_to_this_module():
    """A rename of ``decisions.py`` or a missing include-router should fail
    here, not by 404ing the Discord alerts a second time."""
    assert sole_handler(
        create_app(), "GET", "/dashboard/decisions/{project}/{thread_id}"
    ) == "magickit.web.decisions.decision_page"


def test_index_redirect_is_registered_to_this_module():
    """`/dashboard/decisions` (list URL) stays a redirect stub in 増分 2
    (msg-096 §4). The real list is 増分 3."""
    assert sole_handler(create_app(), "GET", "/dashboard/decisions") == (
        "magickit.web.decisions.decisions_index_redirect"
    )


def test_dashboard_decisions_paths_are_single_handler():
    """The base URL is a shipped contract; a second handler on it would
    make dispatch depend on registration order."""
    table = route_table(create_app())
    for path in (
        "/dashboard/decisions/{project}/{thread_id}",
        "/dashboard/decisions",
    ):
        handlers = table.get(("GET", path), [])
        assert len(handlers) == 1, (
            f"{path} has more than one GET handler: {handlers}"
        )


# --- the index redirect (still stubbed in 増分 2) ------------------------


@pytest.mark.asyncio
async def test_index_redirect_is_302_to_dashboard():
    """`/dashboard/decisions` is still a stub in 増分 2 (msg-096 §4)."""
    r = await _get("/dashboard/decisions")

    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard"
    assert "no-store" in r.headers.get("cache-control", "").lower()


# --- D-26' 4-branch behavior ---------------------------------------------
#
# Each branch is tested by injecting a ChatroomAdapter mock via
# ``chatroom_tools._adapter`` and asserting the status code + template mode.
# The template's textual output is not asserted verbatim (it belongs to the
# renderer's own tests / manual QA); we assert the observable contract
# (status codes / cache directive / that Conclair was consulted).


def _adapter_returning(payload: Any) -> AsyncMock:
    """Build a fake ChatroomAdapter that answers ``get_thread`` with ``payload``."""
    adapter = AsyncMock()
    adapter.get_thread = AsyncMock(return_value=payload)
    adapter.close = AsyncMock()
    return adapter


def _adapter_raising(exc: Exception) -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_thread = AsyncMock(side_effect=exc)
    adapter.close = AsyncMock()
    return adapter


@pytest.mark.asyncio
async def test_branch_parked_to_human_returns_200_and_judgement_ui():
    """Structured `next_participant=human` on the last msg → 200 判断 UI.

    Bohr §2 §1: the first-class parking signal is the structured field
    (PR #28), not a body-line regex. This test pins that ordering.
    """
    adapter = _adapter_returning({
        "thread": {"title": "T-x thread"},
        "messages": [
            {"author": "Bohr", "content": "please decide", "next_participant": "human"},
        ],
        "mode": "full",
    })
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get("/dashboard/decisions/spirrow-magickit/T-x")

    assert r.status_code == 200
    # judgement UI hints (form + parked_author). Not a verbatim assertion --
    # it just proves the branch fired, not what the copy says.
    assert "next_participant" in r.text
    assert "Bohr" in r.text
    assert "no-store" in r.headers.get("cache-control", "").lower()


@pytest.mark.asyncio
async def test_branch_body_fallback_next_human_returns_200_and_judgement_ui():
    """Legacy msg without a structured field, body has single-line NEXT: human."""
    adapter = _adapter_returning({
        "thread": {"title": "T-y"},
        "messages": [
            {"author": "Heisenberg", "content": "here is the situation\n\nNEXT: human"},
        ],
        "mode": "full",
    })
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get("/dashboard/decisions/spirrow-magickit/T-y")

    assert r.status_code == 200
    assert "Heisenberg" in r.text  # parked_author surfaced


@pytest.mark.asyncio
async def test_branch_not_waiting_returns_200_and_link_to_chatroom():
    """Thread exists but is not parked to human → 200 「判断待ちではありません」."""
    adapter = _adapter_returning({
        "thread": {"title": "T-z"},
        "messages": [
            {"author": "Bohr", "content": "handed off", "next_participant": "Heisenberg"},
        ],
        "mode": "full",
    })
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get("/dashboard/decisions/spirrow-magickit/T-z")

    assert r.status_code == 200
    # No form here (this is the "not waiting" template branch).
    assert "判断待ちではありません" in r.text
    # Chatroom link is present.
    assert "/ui/projects/spirrow-magickit/threads/T-z" in r.text


@pytest.mark.asyncio
async def test_branch_thread_not_found_returns_404_not_503():
    """Conclair's explicit "not found" envelope → **404**, not 503.

    Einstein §3 boundary: any error envelope must not be 503-flattened, or
    the 404 branch never fires. This test injects a NotFound-like envelope
    and asserts 404.
    """
    adapter = _adapter_returning({
        "error_type": "ThreadNotFound",
        "error": "no such thread",
    })
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get("/dashboard/decisions/spirrow-magickit/T-nope")

    assert r.status_code == 404


@pytest.mark.asyncio
async def test_branch_generic_error_envelope_returns_503_not_404():
    """A non-NotFound error envelope → **503**, not 404.

    The other side of the boundary: don't invent "not found" when Conclair
    reports a different failure. spec §1 / msg-093 §2 一般則の実体。
    """
    adapter = _adapter_returning({
        "error_type": "ChatroomIntegrityError",
        "error": "integrity check failed",
    })
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get("/dashboard/decisions/spirrow-magickit/T-x")

    assert r.status_code == 503


@pytest.mark.asyncio
async def test_branch_adapter_exception_returns_503():
    """A transport-level failure (Conclair unreachable) → 503, with /ui link."""
    adapter = _adapter_raising(httpx.ConnectError("connection refused"))
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get("/dashboard/decisions/spirrow-magickit/T-x")

    assert r.status_code == 503
    # /ui link surfaces so the user has somewhere to go.
    assert "/ui/projects/spirrow-magickit/threads/T-x" in r.text


@pytest.mark.asyncio
async def test_branch_empty_messages_treated_as_not_waiting():
    """A thread with no messages is not parked (there is no last msg)."""
    adapter = _adapter_returning({
        "thread": {"title": "T-empty"},
        "messages": [],
        "mode": "full",
    })
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get("/dashboard/decisions/spirrow-magickit/T-empty")

    assert r.status_code == 200
    assert "判断待ちではありません" in r.text


# --- URL encoding is preserved through the handler -----------------------


@pytest.mark.asyncio
async def test_percent_signs_in_thread_id_reach_ui_link_intact():
    """The `/ui` link in the response has to survive percent-signs.

    Increment 1's D-C (self-encoded location) still applies to the internal
    ``/ui`` link this handler surfaces on the not_waiting / unavailable
    template modes: we don't hand the URL to a re-quoting helper.
    """
    adapter = _adapter_returning({
        "thread": {"title": "T-x"},
        "messages": [{"author": "Bohr", "content": "hi", "next_participant": "Heisenberg"}],
        "mode": "full",
    })
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get("/dashboard/decisions/p/T%2520foo")  # decodes to T%20foo

    assert r.status_code == 200
    # The link back must re-encode the % as %25 to reach the correct thread.
    assert "/ui/projects/p/threads/T%2520foo" in r.text
