"""Reverse proxy that puts Conclair's ``/ui`` behind Magickit.

Why the proxy exists at all
---------------------------
Conclair binds loopback only ("external access goes through magickit" in
its unit file) and its ``/ui`` is the chatroom's human surface. Serving it
through Magickit gives three things at once:

- **One origin.** ``/dashboard`` (Magickit) and ``/ui`` (Conclair) sit on
  the same host:port, so links between them are plain relative hrefs and
  ``localStorage`` (Conclair keeps the author name there) is shared.
- **No circular dependency.** Writes have to pass the role / naysayer
  gates, which live in Magickit. Having Conclair call Magickit would put
  an edge back against ``magickit.adapters.chatroom``; proxying instead
  keeps Conclair a leaf that knows nothing about Magickit.
- **One exposure point.** ``tailscale serve`` needs to publish a single
  port.

Why the path is preserved verbatim
----------------------------------
Conclair's templates and ``conclair.js`` build every URL as an absolute
path (``/ui/...``, ``/static/...``) and the app has no ``root_path`` /
``url_for`` support. Mounting this proxy under any other prefix would
make the proxied HTML emit links that escape the prefix. So ``/ui/x`` maps
to ``/ui/x`` upstream, unrewritten, and the HTML needs no rewriting either.

The same constraint is why only two static files are proxied rather than
all of ``/static``: Magickit mounts its own ``/static`` for the dashboard
(``dashboard.css`` / ``dashboard.js``). The filenames do not collide, so
the two Conclair assets are forwarded individually and everything else
still falls through to Magickit's own mount.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Request, Response

from magickit.config import get_settings
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["chatroom-ui"])

# Static assets Conclair's templates reference by absolute path. Anything
# not listed here keeps hitting Magickit's own /static mount.
_CONCLAIR_STATIC = (
    "/static/css/conclair.css",
    "/static/js/conclair.js",
)

# Headers that describe the upstream connection or its wire encoding.
# httpx has already decoded the body by the time we build our Response, so
# forwarding these would describe the payload incorrectly.
_DROP_RESPONSE_HEADERS = frozenset(
    {
        "content-encoding",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "server",
        "date",
    }
)

_DROP_REQUEST_HEADERS = frozenset(
    {
        "host",
        "connection",
        "keep-alive",
        "content-length",
        "transfer-encoding",
    }
)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Lazily build the shared upstream client.

    ``follow_redirects=False`` is deliberate: Conclair answers ``/ui`` with
    a 307 to ``/ui/``, and since the path is preserved that Location is
    already correct in our origin. Passing it through lets the browser do
    the hop and keeps the address bar honest.
    """
    global _client
    if _client is None or _client.is_closed:
        settings = get_settings()
        _client = httpx.AsyncClient(
            base_url=settings.conclair_url.rstrip("/"),
            timeout=httpx.Timeout(settings.conclair_timeout),
            follow_redirects=False,
        )
    return _client


async def close_client() -> None:
    """Release the upstream client. Called from the app lifespan."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _forwardable_request_headers(request: Request) -> dict[str, str]:
    """Pass the request through, minus hop-by-hop headers.

    HTMX drives the whole UI, so ``HX-Request`` / ``HX-Target`` and friends
    must survive the hop -- Conclair branches on them to decide between a
    full page and a partial.
    """
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _DROP_REQUEST_HEADERS
    }


def _relativize_location(location: str, upstream_base: str) -> str:
    """Strip the upstream origin off a redirect target.

    Conclair builds absolute redirects (its ``redirect_slashes`` turns
    ``/ui`` into ``http://localhost:8115/ui/``). Forwarded as-is, a remote
    browser would chase Magickit's *own* loopback view of Conclair and get
    nothing. Since the proxy preserves paths one-to-one, dropping the
    origin yields a target that is already correct in our origin.
    """
    if upstream_base and location.startswith(upstream_base):
        return location[len(upstream_base) :] or "/"
    return location


def _proxied_response(upstream: httpx.Response, upstream_base: str) -> Response:
    """Rebuild an upstream response for our client.

    Response headers are forwarded (so ``HX-Trigger`` keeps driving HTMX)
    except the ones that describe the upstream wire format. ``Location`` is
    forwarded too, but rewritten to stay inside our origin.
    """
    headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _DROP_RESPONSE_HEADERS
    }
    if "location" in upstream.headers:
        headers["location"] = _relativize_location(
            upstream.headers["location"], upstream_base
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=headers,
        media_type=upstream.headers.get("content-type"),
    )


async def _proxy(request: Request, upstream_path: str) -> Response:
    client = _get_client()
    try:
        upstream = await client.request(
            request.method,
            upstream_path,
            params=dict(request.query_params),
            headers=_forwardable_request_headers(request),
        )
    except httpx.HTTPError as e:
        logger.error(
            "Conclair UI proxy request failed",
            path=upstream_path,
            method=request.method,
            error=str(e),
        )
        return Response(
            content=(
                "<h1>chatroom UI unavailable</h1>"
                "<p>Conclair did not answer. Check "
                "<code>spirrow-conclair.service</code>.</p>"
            ),
            status_code=502,
            media_type="text/html; charset=utf-8",
        )
    return _proxied_response(upstream, str(client.base_url))


@router.get("/ui")
async def chatroom_ui_root(request: Request) -> Response:
    """Proxy the bare ``/ui`` (Conclair answers it with a 307 to ``/ui/``)."""
    return await _proxy(request, "/ui")


@router.get("/ui/{path:path}")
async def chatroom_ui(request: Request, path: str) -> Response:
    """Proxy every Conclair UI page and HTMX partial, path unchanged."""
    return await _proxy(request, f"/ui/{path}")


@router.get("/static/css/conclair.css")
async def conclair_css(request: Request) -> Response:
    """Forward Conclair's stylesheet past Magickit's own /static mount."""
    return await _proxy(request, _CONCLAIR_STATIC[0])


@router.get("/static/js/conclair.js")
async def conclair_js(request: Request) -> Response:
    """Forward Conclair's script past Magickit's own /static mount."""
    return await _proxy(request, _CONCLAIR_STATIC[1])
