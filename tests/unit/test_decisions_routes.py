"""Unit tests for the 判断 (decision) page redirect stubs (S5' 増分 1).

Scope, said in advance so nobody reads more into a green run than is
there:

* These tests pin **the redirect** -- 302, the exact Location, the
  Cache-Control that stops a shipped URL from being cached, and that
  each path segment is percent-encoded on the way out.
* They do **not** pin arrival: nothing here proves that the Discord
  alert URL reaches a page in production. That is an external-facing
  claim (A-13), and the whole reason 増分 1 exists is that CI cannot
  make it. Arrival is verified out-of-band by curl -L against :8443
  and by a real tap from Discord (msg-085 §2 / msg-087 §3). A green
  file here is necessary; it is not sufficient.
"""

from __future__ import annotations

import httpx
import pytest

from magickit.main import create_app
from tests.route_table import route_table, sole_handler


async def _get(path: str) -> httpx.Response:
    """One-shot ASGI GET that does *not* follow redirects.

    ``follow_redirects=False`` is the point: we are asserting the
    303/302 hop itself, not what it lands on. If httpx followed it we
    would test whatever /ui returns from the TestClient (a proxy that
    is not wired up in this harness), and pass on a broken redirect.
    """
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        return await client.get(path)


# --- route registration --------------------------------------------------


def test_thread_redirect_is_registered_to_this_module():
    """A rename of ``decisions.py`` or a missing include-router should fail
    here, not by 404ing the Discord alerts a second time."""
    assert sole_handler(
        create_app(), "GET", "/dashboard/decisions/{project}/{thread_id}"
    ) == "magickit.web.decisions.decision_redirect"


def test_index_redirect_is_registered_to_this_module():
    assert sole_handler(create_app(), "GET", "/dashboard/decisions") == (
        "magickit.web.decisions.decisions_index_redirect"
    )


def test_redirect_paths_do_not_collide_with_another_handler():
    """The base URL is a shipped contract; a second handler on it would
    make dispatch depend on registration order, which is the same class
    of bug ``test_no_path_is_registered_twice`` catches for the dashboard.
    We check the two paths this module owns directly so a failure names
    the collision instead of a global count."""
    table = route_table(create_app())
    for path in (
        "/dashboard/decisions/{project}/{thread_id}",
        "/dashboard/decisions",
    ):
        handlers = table.get(("GET", path), [])
        assert len(handlers) == 1, (
            f"{path} has more than one GET handler: {handlers}"
        )


# --- the redirect itself -------------------------------------------------


@pytest.mark.asyncio
async def test_thread_redirect_is_302_not_permanent():
    """301 / 308 must not be used here. 増分 2 rebuilds the real page on
    this same URL, and a permanent redirect cached by a mobile browser or
    a Service Worker would ship users to ``/ui`` forever with no
    server-side way to correct it (Bohr D-B, Einstein Q1)."""
    r = await _get("/dashboard/decisions/spirrow-voxelworld/T-lod0-sliver-shards")

    assert r.status_code == 302


@pytest.mark.asyncio
async def test_thread_redirect_points_at_the_ui_thread_page():
    r = await _get("/dashboard/decisions/spirrow-voxelworld/T-lod0-sliver-shards")

    assert r.headers["location"] == (
        "/ui/projects/spirrow-voxelworld/threads/T-lod0-sliver-shards"
    )


@pytest.mark.asyncio
async def test_thread_redirect_forbids_caching():
    """The directive that plugs the hole is ``no-store``: nothing is kept,
    so nothing outlives 増分 2's release. ``must-revalidate`` may also be
    there, but this assertion pins the one doing the work."""
    r = await _get("/dashboard/decisions/spirrow-voxelworld/T-lod0-sliver-shards")

    assert "no-store" in r.headers.get("cache-control", "").lower()


@pytest.mark.asyncio
async def test_thread_redirect_percent_encodes_each_segment():
    """A ``thread_id`` containing a percent sign has to arrive at ``/ui``
    as ``%25`` -- not as ``%``. This test uses ``T%20foo`` (a literal
    percent-two-zero) because a browser will never send that shape
    accidentally, so if it round-trips unchanged we would notice."""
    r = await _get("/dashboard/decisions/p/T%2520foo")

    # ``T%2520foo`` on the wire decodes to the string ``T%20foo`` inside
    # FastAPI. Re-encoding that with safe="" gives ``T%2520foo`` again,
    # which is what the thread page has to see to serve the right room.
    assert r.headers["location"] == "/ui/projects/p/threads/T%2520foo"


@pytest.mark.asyncio
async def test_thread_redirect_does_not_leak_query_boundary_characters():
    """The exact fear behind D-C: ``RedirectResponse``'s ``safe`` set
    includes ``?`` ``&`` ``#``, so a thread id ever containing one would
    split the URL on it. We build the ``Location`` ourselves; this test
    pins that a ``?`` in the segment stays inside the segment."""
    # ``%3F`` decodes to ``?`` inside FastAPI. It must re-encode to
    # ``%3F`` in the Location, not appear as a literal ``?`` that would
    # start a query string.
    r = await _get("/dashboard/decisions/p/T%3Fx")

    assert r.headers["location"] == "/ui/projects/p/threads/T%3Fx"
    assert "?" not in r.headers["location"]


@pytest.mark.asyncio
async def test_index_redirect_is_302_to_dashboard():
    """The list URL (``/dashboard/decisions``) has no page yet -- 増分 3
    builds it. Until then send the visitor somewhere with signal (ops)
    rather than a 404 that reads as "the feature does not exist"."""
    r = await _get("/dashboard/decisions")

    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard"
    assert "no-store" in r.headers.get("cache-control", "").lower()


@pytest.mark.asyncio
async def test_redirect_does_not_check_thread_existence():
    """Increment 1 must not consult Conclair. Doing so would put the
    revived link back under Conclair's uptime, which is worse than
    today's failure (Bohr D-D). If this test ever fails, someone has
    added an adapter call to a stub whose entire value is that it does
    not have any."""
    # If the handler talked to Conclair, the ASGI request would either
    # hang or 500 -- the test harness has no Conclair. A 302 in under a
    # second, for a project name Conclair has never heard of, proves the
    # handler did not ask.
    r = await _get("/dashboard/decisions/does-not-exist/T-also-not")

    assert r.status_code == 302
    assert r.headers["location"] == (
        "/ui/projects/does-not-exist/threads/T-also-not"
    )
