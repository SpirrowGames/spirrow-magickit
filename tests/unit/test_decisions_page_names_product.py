"""The 判断 page says which product the thread belongs to.

This page is opened from the board (/dashboard/decisions) and from
Discord alerts, so the reader arrives without the surrounding context.
Until now nothing on the page named the product -- the id was in the URL
and in the chatroom link's href, and nowhere a reader looks.

The eyebrow is rendered once, outside the mode branches, because every
mode's context carries ``project``; these tests pin it on all four so a
new mode cannot quietly drop it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.main import create_app
from magickit.mcp.tools import chatroom as chatroom_tools


@pytest.fixture(autouse=True)
def _configured():
    chatroom_tools.configure(Settings())
    yield
    chatroom_tools._settings = None


async def _get(path: str) -> httpx.Response:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        return await client.get(path)


def _adapter_returning(payload: Any) -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_thread = AsyncMock(return_value=payload)
    adapter.close = AsyncMock()
    return adapter


def _adapter_raising(exc: Exception) -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_thread = AsyncMock(side_effect=exc)
    adapter.close = AsyncMock()
    return adapter


def _eyebrow(project: str) -> str:
    """The rendered eyebrow.

    Asserted as markup rather than as a bare substring: the project id
    already appears in hrefs on these pages, so `project in r.text` would
    pass with the eyebrow deleted.
    """
    return f'<p class="decision-product">{project}</p>'


@pytest.mark.asyncio
async def test_judgement_page_names_the_product():
    adapter = _adapter_returning({
        "thread": {"title": "T-x thread", "status": "active"},
        "messages": [
            {"author": "Bohr", "content": "please decide", "next_participant": "human"},
        ],
        "mode": "full",
    })
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get("/dashboard/decisions/spirrow-mindwire/T-x")

    assert r.status_code == 200
    assert _eyebrow("spirrow-mindwire") in r.text


@pytest.mark.asyncio
async def test_not_waiting_page_names_the_product():
    adapter = _adapter_returning({
        "thread": {"title": "T-z", "status": "active"},
        "messages": [
            {"author": "Bohr", "content": "handed off", "next_participant": "Heisenberg"},
        ],
        "mode": "full",
    })
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get("/dashboard/decisions/spirrow-mindwire/T-z")

    assert r.status_code == 200
    assert "判断待ちではありません" in r.text
    assert _eyebrow("spirrow-mindwire") in r.text


@pytest.mark.asyncio
async def test_not_found_page_names_the_product():
    """404 is exactly where the product matters: the reader has to decide
    whether the URL is wrong or the thread is gone."""
    adapter = _adapter_returning({
        "error_type": "ThreadNotFound",
        "error": "no such thread",
    })
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get("/dashboard/decisions/spirrow-mindwire/T-nope")

    assert r.status_code == 404
    assert _eyebrow("spirrow-mindwire") in r.text


@pytest.mark.asyncio
async def test_unavailable_page_names_the_product():
    """Conclair being unreachable does not cost us the product name --
    it comes from the URL, not from the fetch."""
    adapter = _adapter_raising(RuntimeError("conclair down"))
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get("/dashboard/decisions/spirrow-mindwire/T-x")

    assert r.status_code == 503
    assert _eyebrow("spirrow-mindwire") in r.text


@pytest.mark.asyncio
async def test_tab_title_names_the_product():
    """Several of these pages are open at once; the tab has to distinguish
    them, and 判断 — T-xxx alone does not."""
    adapter = _adapter_returning({
        "thread": {"title": "T-x thread", "status": "active"},
        "messages": [
            {"author": "Bohr", "content": "please decide", "next_participant": "human"},
        ],
        "mode": "full",
    })
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get("/dashboard/decisions/spirrow-mindwire/T-x")

    assert "<title>判断 — spirrow-mindwire / T-x thread</title>" in r.text


@pytest.mark.asyncio
async def test_product_name_is_escaped():
    """The id is a path segment, so it is reader-supplied. Autoescape owns
    this (the page uses no |safe); pinned because the eyebrow is the one
    place a raw id reaches the document outside an href.

    No %2F in the fixture: an encoded slash never reaches the route at
    all, so it would test the router rather than the escaping.
    """
    adapter = _adapter_returning({
        "error_type": "ThreadNotFound",
        "error": "no such thread",
    })
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get("/dashboard/decisions/%3Cb%3Eoops/T-x")

    assert r.status_code == 404
    assert "<b>oops" not in r.text
    assert "&lt;b&gt;oops" in r.text
