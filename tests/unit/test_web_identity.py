"""What the tailnet identity headers are, and are not, worth.

These are the checks standing between "reached the dashboard" and
"restarted production", so they are written as properties rather than as
coverage. The measurements they encode are in
``magickit.web.identity``'s docstring; what is pinned here is the
behaviour that follows from them.
"""

from __future__ import annotations

from starlette.requests import Request

from magickit.web import identity

APPROVERS = ["takayan0908@gmail.com"]


def _request(**headers: str) -> Request:
    raw = [(k.lower().replace("_", "-").encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/dashboard/deploys/req-1/approve",
        "headers": raw,
        "query_string": b"",
    }
    return Request(scope)


# --- who is vouched for --------------------------------------------------


def test_a_tailnet_user_is_recognised() -> None:
    req = _request(**{"tailscale-user-login": "takayan0908@gmail.com"})

    assert identity.tailnet_login(req) == "takayan0908@gmail.com"
    assert identity.is_approver(req, APPROVERS) is True


def test_no_identity_header_is_a_denial_not_an_anonymous_user() -> None:
    """A request the proxy did not vouch for gets nothing.

    This is the case for anything that reaches the app without going
    through ``tailscale serve`` -- which, with the loopback bind, means a
    process already on the host. Those have their own door.
    """
    req = _request(host="localhost:8113")

    assert identity.tailnet_login(req) is None
    assert identity.is_approver(req, APPROVERS) is False


def test_a_tagged_device_cannot_approve() -> None:
    """The invariant the whole feature is built around.

    ``sg-tomtebo-01`` runs the development loop and is a tagged device,
    so ``tailscale serve`` attaches no user login for it. An allowlist of
    user logins therefore excludes it by construction, exactly as the
    OAuth door does by not registering `deploy_approve`.
    """
    req = _request(**{"tailscale-user-name": "sg-tomtebo-01"})  # no user login

    assert identity.tailnet_login(req) is None
    assert identity.is_approver(req, APPROVERS) is False


def test_a_tailnet_user_who_is_not_on_the_list_cannot_approve() -> None:
    """The tailnet has more than one human; being on it is not consent."""
    req = _request(**{"tailscale-user-login": "dany1468@example.com"})

    assert identity.is_approver(req, APPROVERS) is False


def test_an_empty_allowlist_admits_nobody() -> None:
    """Fail closed: a deployment that named no approver keeps the
    read-only page it had before this feature existed."""
    req = _request(**{"tailscale-user-login": "takayan0908@gmail.com"})

    assert identity.is_approver(req, []) is False
    assert identity.is_approver(req, ["", "   "]) is False


def test_the_login_comparison_ignores_case_and_padding() -> None:
    """Config is hand-written; a capital letter must not silently revoke
    someone's ability to approve."""
    req = _request(**{"tailscale-user-login": "  TakaYan0908@Gmail.com  "})

    assert identity.is_approver(req, ["takayan0908@gmail.com"]) is True
    assert identity.is_approver(req, ["  TAKAYAN0908@GMAIL.COM "]) is True


# --- whose intent is it --------------------------------------------------


def test_a_cross_site_post_is_refused() -> None:
    """The header says whose browser it is, not whose idea it was.

    Any page Takahito visits while on the tailnet can POST here, and the
    proxy will attach his real identity to it.
    """
    req = _request(
        **{"tailscale-user-login": "takayan0908@gmail.com", "sec-fetch-site": "cross-site"}
    )

    assert identity.cross_site(req) is True


def test_a_same_origin_form_post_is_allowed() -> None:
    req = _request(
        **{
            "tailscale-user-login": "takayan0908@gmail.com",
            "sec-fetch-site": "same-origin",
            "origin": "https://sg-ai-server-01.taile861db.ts.net:8443",
            "host": "sg-ai-server-01.taile861db.ts.net:8443",
        }
    )

    assert identity.cross_site(req) is False


def test_an_origin_naming_someone_else_is_refused_even_without_fetch_metadata() -> None:
    """Belt and braces: `Sec-Fetch-Site` is the primary signal, but a
    request that names a foreign origin is answered on that alone."""
    req = _request(
        **{
            "tailscale-user-login": "takayan0908@gmail.com",
            "origin": "https://evil.example",
            "host": "sg-ai-server-01.taile861db.ts.net:8443",
        }
    )

    assert identity.cross_site(req) is True


def test_a_non_browser_client_is_not_treated_as_cross_site() -> None:
    """No fetch metadata and no Origin means no browser, so there is no
    page to have been tricked. Such a caller still has to satisfy
    `is_approver`, which no attacker's page can."""
    req = _request(**{"tailscale-user-login": "takayan0908@gmail.com"})

    assert identity.cross_site(req) is False


def test_same_site_is_still_not_same_origin() -> None:
    """A sibling host under the same registrable domain is a different
    page; `same-site` is not good enough for something that restarts a
    service."""
    req = _request(
        **{"tailscale-user-login": "takayan0908@gmail.com", "sec-fetch-site": "same-site"}
    )

    assert identity.cross_site(req) is True
