"""The deploy page: what it shows, and who it lets act.

The load-bearing tests are at the end. This page used to have no write
route at all, because the app is the tailnet front door and approval
restarts services -- and a form added on those terms would have been a
convenience that handed production to anyone who could reach the port.

That reasoning has not been dropped, it has been *answered*: the app now
binds loopback, so the only way in is `tailscale serve`, which attaches a
tailnet identity the client cannot forge (measured -- see
`magickit.web.identity`). So the invariant under test changed shape
rather than strength:

- before: "there is no route that writes";
- now: "the route that writes is unreachable without a named user from
  an allowlist, and the page shows no control to anyone else."

The default page -- no identity, empty allowlist -- must still render
exactly as it did, and `test_the_page_offers_no_way_to_approve_or_deploy`
still says so.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from magickit.config import Settings
from magickit.deploy import records
from magickit.web import deploys as deploys_module
from magickit.web.deploys import router

APPROVER = "takayan0908@gmail.com"

#: Headers a browser gets when it reaches the page through
#: `tailscale serve` as an allowlisted user. Same-origin, because that is
#: what a form on our own page produces.
def _approver_headers(login: str = APPROVER) -> dict[str, str]:
    return {
        "Tailscale-User-Login": login,
        "Sec-Fetch-Site": "same-origin",
        "Host": "sg-ai-server-01.taile861db.ts.net:8443",
        "Origin": "https://sg-ai-server-01.taile861db.ts.net:8443",
    }


@pytest.fixture(autouse=True)
def launched(monkeypatch):
    """Make it impossible for a test in this file to start a real deploy.

    Not a convenience. `approve_request` ends in `launcher.launch`, which
    runs `systemd-run --user` for real, and patching `default_state_root`
    does not reach that subprocess -- it resolves the *live* state
    directory on its own. An approve test written without this stub does
    spawn a runner unit against production state; observed doing exactly
    that while this file was being written, saved only by the subprocess
    failing to find the tmp-path record.

    Autouse, so the protection cannot be forgotten by the next test.
    """
    calls: list[str] = []

    def _fake_launch(request_id: str) -> tuple[bool, str]:
        calls.append(request_id)
        return True, f"magickit-deploy-{request_id}.service"

    monkeypatch.setattr(deploys_module.approval.launcher, "launch", _fake_launch)
    return calls


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    monkeypatch.setattr(records, "default_state_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def approvers(monkeypatch):
    """Control the allowlist rather than inheriting the repo's config.

    `get_settings()` reads `config/magickit_config.yaml` from the working
    directory, so without this the suite would quietly assert against
    whatever this checkout happens to ship.
    """

    def _set(*logins: str) -> None:
        monkeypatch.setattr(
            deploys_module,
            "get_settings",
            lambda: Settings(deploy_approver_logins=list(logins)),
        )

    _set()  # default: nobody, which is the shipped default
    return _set


@pytest.fixture
def client(state_root, approvers) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def pending(state_root):
    """One request waiting for a decision."""
    store = records.DeployStore(state_root)
    request = store.create(target="spirrow-conclair", requested_by="loop", reason="r")
    store.save(request)
    return request


def test_the_page_renders_with_no_history_at_all(client):
    response = client.get("/dashboard/deploys")

    assert response.status_code == 200
    assert "まだ 1 件も要求されていません" in response.text


def test_the_allowlist_is_shown_so_the_reader_knows_the_bounds(client):
    body = client.get("/dashboard/deploys").text

    assert "spirrow-conclair" in body
    assert "spirrow-lexora" in body
    # The one that is refused must not be advertised as deployable.
    assert "spirrow-magickit" not in body


def test_a_finished_deploy_shows_the_sha_and_what_is_serving(client, state_root):
    store = records.DeployStore(state_root)
    request = store.create(target="spirrow-conclair", requested_by="loop", reason="r")
    request.status = records.STATUS_SUCCEEDED
    request.approved_by = "Takahito"
    request.result = {
        "deployed_sha": "a" * 40,
        "service_state": records.SERVICE_UP_NEW,
    }
    store.save(request)

    body = client.get("/dashboard/deploys").text

    assert "a" * 12 in body
    assert records.SERVICE_UP_NEW in body
    assert "Takahito" in body


def test_the_page_says_which_door_an_approval_came_through(client, state_root):
    """"who approved" and "how were they vouched for" are different
    questions, and the page answers both or neither."""
    store = records.DeployStore(state_root)
    request = store.create(target="spirrow-conclair", requested_by="loop", reason="r")
    request.approved_by = "Takahito"
    request.approved_via = "host-cli"
    store.save(request)

    body = client.get("/dashboard/deploys").text

    assert "Takahito" in body
    assert "host-cli" in body


def test_a_failed_deploy_shows_its_error(client, state_root):
    store = records.DeployStore(state_root)
    request = store.create(target="spirrow-conclair", requested_by="loop", reason="r")
    request.status = records.STATUS_FAILED
    request.result = {"error": "the backup failed", "service_state": records.SERVICE_DOWN}
    store.save(request)

    body = client.get("/dashboard/deploys").text

    assert "the backup failed" in body
    assert records.SERVICE_DOWN in body


def test_an_override_is_visible_rather_than_buried(client, state_root):
    store = records.DeployStore(state_root)
    request = store.create(target="spirrow-conclair", requested_by="loop", reason="r")
    request.override_ref = "fix/hotfix"
    request.override_reason = "prod down"
    store.save(request)

    assert "override" in client.get("/dashboard/deploys").text


def test_the_audit_trail_is_shown(client, state_root):
    store = records.DeployStore(state_root)
    store.audit("approved", request_id="a", target="spirrow-conclair", actor="Takahito")

    body = client.get("/dashboard/deploys").text

    assert "approved" in body
    assert "Takahito" in body


def test_the_page_offers_no_way_to_approve_or_deploy(client, pending):
    """A reader the proxy did not vouch for sees the page it always was.

    This is the original invariant, unchanged: no identity, no control.
    It is the shipped default too, because the allowlist starts empty.
    """
    body = client.get("/dashboard/deploys").text

    assert "<form" not in body.lower()
    assert "hx-post" not in body.lower()
    assert "承認して開始" not in body


# --- the approve door ----------------------------------------------------
#
# `magickit.web.identity` has the unit tests for the predicates. What is
# checked here is that the route actually consults them, and that a
# refusal leaves the record untouched -- a gate that returns 403 after
# launching a deploy would pass a predicate test and still be a disaster.


def _pending_status(state_root, request_id: str) -> str:
    return records.DeployStore(state_root).load(request_id).status


def test_an_unvouched_post_is_refused_and_changes_nothing(
    client, pending, state_root, launched
):
    """The route is reachable; the capability is not."""
    response = client.post(f"/dashboard/deploys/{pending.request_id}/approve")

    assert response.status_code == 403
    assert _pending_status(state_root, pending.request_id) == records.STATUS_PENDING
    assert launched == []


def test_a_tailnet_user_who_is_not_an_approver_is_refused(
    client, pending, state_root, approvers, launched
):
    """Being on the tailnet is not being on the list; the tailnet has
    more than one human."""
    approvers(APPROVER)

    response = client.post(
        f"/dashboard/deploys/{pending.request_id}/approve",
        headers=_approver_headers("someone-else@example.com"),
    )

    assert response.status_code == 403
    assert "承認者リストにありません" in response.text
    assert _pending_status(state_root, pending.request_id) == records.STATUS_PENDING
    assert launched == []


def test_the_refusal_says_which_problem_it_was(client, pending, approvers):
    """"no identity" and "not on the list" have different fixes, so the
    page must not collapse them into one message."""
    approvers(APPROVER)

    body = client.post(f"/dashboard/deploys/{pending.request_id}/approve").text

    assert "tailnet identity が付いていません" in body


def test_a_cross_site_post_is_refused_even_with_a_valid_identity(
    client, pending, state_root, approvers, launched
):
    """The proxy attaches a real identity to a forged request too. This
    is the one refusal that cannot be spotted by looking at who."""
    approvers(APPROVER)
    headers = _approver_headers() | {
        "Sec-Fetch-Site": "cross-site",
        "Origin": "https://evil.example",
    }

    response = client.post(
        f"/dashboard/deploys/{pending.request_id}/approve", headers=headers
    )

    assert response.status_code == 403
    assert "別サイト" in response.text
    assert _pending_status(state_root, pending.request_id) == records.STATUS_PENDING
    assert launched == []


def test_an_approver_sees_the_control_only_for_pending_requests(
    client, state_root, approvers
):
    approvers(APPROVER)
    store = records.DeployStore(state_root)
    waiting = store.create(target="spirrow-conclair", requested_by="loop", reason="r")
    store.save(waiting)
    done = store.create(target="spirrow-lexora", requested_by="loop", reason="r")
    done.status = records.STATUS_SUCCEEDED
    store.save(done)

    body = client.get("/dashboard/deploys", headers=_approver_headers()).text

    assert body.count("承認して開始") == 1
    assert f"/dashboard/deploys/{waiting.request_id}/approve" in body
    assert f"/dashboard/deploys/{done.request_id}/approve" not in body


def test_an_approver_is_told_that_they_are_one(client, approvers):
    """The page states the basis for the control it is showing, so an
    unexpected button is traceable to an identity rather than a mystery."""
    approvers(APPROVER)

    body = client.get("/dashboard/deploys", headers=_approver_headers()).text

    assert APPROVER in body


def test_approval_records_the_identity_and_the_door(
    client, pending, state_root, approvers, launched
):
    """The audit line is the point of routing this through the mechanism.

    `approved_by` is the vouched-for identity rather than a form field:
    the other doors take a name because something else vouched for the
    actor, and here the identity *is* the vouching.
    """
    approvers(APPROVER)

    response = client.post(
        f"/dashboard/deploys/{pending.request_id}/approve",
        data={"note": "手元で確認済み"},
        headers=_approver_headers(),
    )

    assert response.status_code == 200
    assert launched == [pending.request_id]

    stored = records.DeployStore(state_root).load(pending.request_id)
    assert stored.approved_by == APPROVER
    assert stored.approved_via == "tailnet-identity"
    assert stored.approval_note == "手元で確認済み"


def test_the_form_cannot_smuggle_a_ref_override(client, pending, state_root, approvers):
    """R-1/R-2 stay on the doors that make you write a reason. Extra form
    fields must not become a quieter way to reach them."""
    approvers(APPROVER)

    client.post(
        f"/dashboard/deploys/{pending.request_id}/approve",
        data={
            "note": "n",
            "override_ref": "fix/whatever",
            "override_allows_migration": "true",
        },
        headers=_approver_headers(),
    )

    stored = records.DeployStore(state_root).load(pending.request_id)
    assert stored.override_ref is None
    assert stored.override_allows_migration is False
