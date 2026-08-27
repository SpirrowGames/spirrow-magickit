"""Who is on the other end of a dashboard request, when that is knowable.

This app has no login. ADR-2026-06-04-18 accepted that on a single-user
tailnet, on the condition that it be reconsidered if the capability
behind it grew -- and approving a deploy is exactly that growth, since it
restarts services, runs migrations, and starts an agent as ``sgadmin``
(``NOPASSWD: ALL``). So approval needs an actor, not just a network.

``tailscale serve`` already knows the actor. It terminates TLS for
``:8443`` and injects the tailnet identity of the peer::

    Tailscale-User-Login: someone@example.com
    Tailscale-User-Name:  someone

**The headers are only evidence because the app is not reachable except
through that proxy.** Measured 2026-08-27 on this host:

- through ``tailscale serve``, a client-supplied
  ``Tailscale-User-Login: attacker@evil.example`` was **overwritten**
  with the peer's real identity;
- straight to the backend port, the same forged header **arrived
  untouched**.

So the loopback bind is not hardening added alongside this feature; it is
the feature's only reason to be believed. ``server.host`` is
``127.0.0.1`` and ``start.sh`` binds the same, and if either is ever put
back to ``0.0.0.0`` every check in this module becomes decorative --
anyone who can reach the port can name themselves. That is what
``tests/unit/test_web_identity.py`` pins, and why the docstring says it
twice.

What this does **not** claim:

- It is not authorization for anything else. Every other page stays as
  it was: unauthenticated, because what they expose is still "write a
  row", which is what the ADR actually weighed.
- It does not make ``sgadmin`` on this host accountable. A shell here can
  still approve through ``python -m magickit.deploy.approval`` or skip
  magickit entirely. That door is deliberate and documented in
  :mod:`magickit.deploy.approval`; this one is for the person holding a
  browser, who until now had to leave the page to act on what it showed.
- It is not a second factor. A stolen tailnet device is a stolen
  approver.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.requests import Request

#: Set by ``tailscale serve`` for a tailnet peer that is a *user*. A
#: tagged device -- ``sg-tomtebo-01``, which runs the development loop --
#: has no user login, so it never matches an allowlist entry. That is the
#: property this whole module exists to buy: the loop cannot approve its
#: own deploy, which is the same invariant the OAuth door protects and
#: the reason `deploy_approve` is absent from the local MCP instance.
LOGIN_HEADER = "tailscale-user-login"
NAME_HEADER = "tailscale-user-name"


def tailnet_login(request: Request) -> str | None:
    """The tailnet identity the proxy vouched for, or ``None``.

    ``None`` means "nobody was vouched for" and callers must treat it as
    a denial rather than as an anonymous user, because the two are the
    same thing here.
    """
    login = (request.headers.get(LOGIN_HEADER) or "").strip()
    return login.lower() or None


def tailnet_name(request: Request) -> str:
    """A display name for the audit line; falls back to the login."""
    name = (request.headers.get(NAME_HEADER) or "").strip()
    return name or (tailnet_login(request) or "unknown")


def is_approver(request: Request, allowed: list[str]) -> bool:
    """Is this request from someone allowed to approve a deploy?

    The allowlist defaults to empty, so a deployment that never
    configured one cannot approve from the browser at all. Fail-closed is
    the only safe default for a list whose job is to name the people who
    may restart production.
    """
    login = tailnet_login(request)
    if login is None:
        return False
    return login in {entry.strip().lower() for entry in allowed if entry.strip()}


def cross_site(request: Request) -> bool:
    """Was this POST driven by a page other than ours?

    The identity header proves *who* the browser belongs to, not *what
    asked it to send this*. Without this check any page Takahito visits
    while on the tailnet could POST to the approve route, and
    ``tailscale serve`` would faithfully attach his identity to the
    forgery -- the header does its job and the deploy still is not his
    idea.

    Both signals are advisory-if-absent and authoritative-if-present:

    - ``Sec-Fetch-Site`` is emitted by every current browser and cannot
      be suppressed by the page that triggered the request, so anything
      other than ``same-origin`` is a refusal.
    - ``Origin`` is sent on cross-origin POSTs; when present it must name
      us.

    Absent both, this is not a browser, and the CSRF question does not
    arise -- a scripted client on the tailnet still had to get past
    :func:`is_approver`, which no page in a victim's browser can do.
    """
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site and site != "same-origin":
        return True

    origin = (request.headers.get("origin") or "").strip()
    if origin:
        # Compare against the host the client actually addressed, which
        # behind the proxy is the tailnet name, not the bind address.
        host = (request.headers.get("host") or "").strip().lower()
        if urlsplit(origin).netloc.lower() != host:
            return True

    return False


__all__ = [
    "LOGIN_HEADER",
    "NAME_HEADER",
    "cross_site",
    "is_approver",
    "tailnet_login",
    "tailnet_name",
]
