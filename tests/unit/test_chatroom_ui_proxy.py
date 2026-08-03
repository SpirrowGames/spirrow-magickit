"""Unit tests for the Conclair ``/ui`` reverse proxy.

The proxy's whole value rests on two invariants that are easy to break by
accident, so both are pinned here rather than left to manual browsing:

  - **Paths pass through unrewritten.** Conclair's templates emit absolute
    ``/ui/...`` and ``/static/...`` URLs and the app has no ``root_path``
    support, so any prefix rewriting would produce HTML whose links escape
    the proxy.
  - **Route order beats the ``/static`` mount.** Magickit mounts its own
    ``/static``; the two Conclair assets must be claimed before it.

Upstream is a ``MockTransport`` so the suite never needs a live Conclair.
"""

from __future__ import annotations

import httpx
import pytest

from magickit.main import create_app
from magickit.web import chatroom_proxy


@pytest.fixture(autouse=True)
def _reset_client():
    """Keep the module-level client from leaking between tests."""
    chatroom_proxy._client = None
    yield
    chatroom_proxy._client = None


def _install_upstream(handler) -> None:
    """Point the proxy at an in-memory upstream."""
    chatroom_proxy._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://localhost:8115",
        follow_redirects=False,
    )


async def _get(path: str, **kwargs) -> httpx.Response:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        return await client.get(path, **kwargs)


# --- path preservation --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested,expected_upstream",
    [
        ("/ui", "/ui"),
        ("/ui/", "/ui/"),
        ("/ui/projects/p/threads", "/ui/projects/p/threads"),
        ("/ui/projects/p/threads/T-x/_messages", "/ui/projects/p/threads/T-x/_messages"),
        ("/static/css/conclair.css", "/static/css/conclair.css"),
        ("/static/js/conclair.js", "/static/js/conclair.js"),
    ],
)
async def test_path_is_forwarded_verbatim(requested: str, expected_upstream: str):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, text="ok")

    _install_upstream(handler)
    response = await _get(requested)

    assert response.status_code == 200
    assert seen == [expected_upstream]


@pytest.mark.asyncio
async def test_query_string_survives_the_hop():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, text="ok")

    _install_upstream(handler)
    await _get("/ui/projects/p/threads", params={"status": "active", "page": "2"})

    assert seen == {"status": "active", "page": "2"}


# --- route ordering vs. Magickit's own /static --------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "asset", ["/static/css/conclair.css", "/static/js/conclair.js"]
)
async def test_conclair_assets_win_over_magickit_static_mount(asset: str):
    """Conclair's two assets must reach the proxy, not Magickit's own mount.

    Asserted through the response rather than by reading route order:
    Starlette's routing internals differ enough between versions that
    introspection here has been fragile, while the behaviour is the actual
    invariant -- if the mount won, the UI would load unstyled.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="/* from conclair */")

    _install_upstream(handler)
    response = await _get(asset)

    assert response.status_code == 200
    assert response.text == "/* from conclair */"


@pytest.mark.asyncio
async def test_magickit_own_static_is_not_swallowed_by_the_proxy():
    """Only the two Conclair files are claimed; dashboard assets stay local."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, text="upstream")

    _install_upstream(handler)
    await _get("/static/css/dashboard.css")

    assert called is False


# --- redirects ----------------------------------------------------------


@pytest.mark.parametrize(
    "location,expected",
    [
        ("http://localhost:8115/ui/", "/ui/"),
        ("http://localhost:8115/ui/x?a=1", "/ui/x?a=1"),
        ("/ui/already-relative", "/ui/already-relative"),
        # An off-origin redirect is not ours to rewrite.
        ("https://elsewhere.example/x", "https://elsewhere.example/x"),
    ],
)
def test_relativize_location(location: str, expected: str):
    assert (
        chatroom_proxy._relativize_location(location, "http://localhost:8115")
        == expected
    )


@pytest.mark.asyncio
async def test_redirect_does_not_leak_the_upstream_origin():
    """A remote browser must never be pointed at Magickit's loopback view."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": "http://localhost:8115/ui/"})

    _install_upstream(handler)
    response = await _get("/ui")

    assert response.status_code == 307
    assert response.headers["location"] == "/ui/"


# --- header handling ----------------------------------------------------


@pytest.mark.asyncio
async def test_htmx_request_headers_reach_conclair():
    """Conclair branches on HX-Request to return a partial instead of a page."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, text="ok")

    _install_upstream(handler)
    await _get("/ui/projects/p/threads/_rows", headers={"HX-Request": "true"})

    assert seen.get("hx-request") == "true"


@pytest.mark.asyncio
async def test_hx_trigger_response_header_is_forwarded():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok", headers={"HX-Trigger": "messagePosted"})

    _install_upstream(handler)
    response = await _get("/ui/projects/p/threads/T-x/_messages")

    assert response.headers["hx-trigger"] == "messagePosted"


@pytest.mark.asyncio
async def test_wire_encoding_headers_are_not_forwarded():
    """httpx already decoded the body; echoing content-encoding corrupts it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="ok",
            headers={"content-encoding": "gzip", "transfer-encoding": "chunked"},
        )

    _install_upstream(handler)
    response = await _get("/ui/")

    assert "content-encoding" not in response.headers
    assert "transfer-encoding" not in response.headers


# --- upstream failure ---------------------------------------------------


@pytest.mark.asyncio
async def test_upstream_outage_surfaces_as_502():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("conclair is down")

    _install_upstream(handler)
    response = await _get("/ui/")

    assert response.status_code == 502
    assert "chatroom UI unavailable" in response.text


@pytest.mark.asyncio
async def test_upstream_error_status_is_passed_through():
    """Conclair's own 404 must not be laundered into a 200 or a 502."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="no such thread")

    _install_upstream(handler)
    response = await _get("/ui/projects/p/threads/nope")

    assert response.status_code == 404


# --- loop control POST --------------------------------------------------
#
# The one POST this proxy carries. Chatroom writes are claimed by
# chatroom_writes ahead of it; loop control has no gate to run, so it is
# forwarded. Without the route the widget's buttons 405 through Magickit --
# which is the tailnet front door the feature is reached from.


async def _post(path: str, **kwargs) -> httpx.Response:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        return await client.post(path, **kwargs)


@pytest.mark.asyncio
async def test_loop_control_post_is_forwarded_with_its_body():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["method"] = request.method
        seen["body"] = request.content.decode()
        seen["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, text="<section id='control-widget'></section>")

    _install_upstream(handler)
    response = await _post(
        "/ui/projects/spirrow-voxelworld/control",
        data={"state": "hold", "author": "takahito"},
    )

    assert response.status_code == 200
    assert seen["method"] == "POST"
    assert seen["path"] == "/ui/projects/spirrow-voxelworld/control"
    # The form body has to survive the hop, or the state never changes.
    assert "state=hold" in seen["body"]
    assert "author=takahito" in seen["body"]
    assert seen["content_type"] == "application/x-www-form-urlencoded"


@pytest.mark.asyncio
async def test_loop_control_post_returns_the_widget_partial():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<section id='control-widget'>HOLD</section>")

    _install_upstream(handler)
    response = await _post(
        "/ui/projects/p/control", data={"state": "hold", "author": "takahito"}
    )

    assert "control-widget" in response.text


@pytest.mark.parametrize(
    "path,expected_module",
    [
        # Loop control: the proxy owns it (nothing to gate).
        ("/ui/projects/{project}/control", "magickit.web.chatroom_proxy"),
        # Chatroom writes: still claimed ahead of the proxy, so the role /
        # naysayer / embodiment gates keep running. Adding a POST route to
        # the proxy must not have widened it into these paths.
        (
            "/ui/projects/{project}/threads/{thread_id}/messages",
            "magickit.web.chatroom_writes",
        ),
        ("/ui/projects/{project}/threads", "magickit.web.chatroom_writes"),
        (
            "/ui/projects/{project}/threads/{thread_id}/close",
            "magickit.web.chatroom_writes",
        ),
    ],
)
def test_post_routes_are_owned_by_the_right_module(path: str, expected_module: str):
    app = create_app()

    owners = [
        route.endpoint.__module__
        for route in app.routes
        if getattr(route, "path", None) == path and "POST" in getattr(route, "methods", ())
    ]

    assert owners, f"no POST route registered for {path}"
    # Starlette matches in insertion order, so the first registration wins.
    assert owners[0] == expected_module
