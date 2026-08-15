"""The deploy page: it shows, and it must not do.

The load-bearing test is the last one. This app is the tailnet front
door and is unauthenticated; approval lives on the OAuth-gated MCP
instance precisely so that the unauthenticated surface cannot restart a
service. A form on this page would undo that in one commit, and it would
look like a convenience.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from magickit.deploy import records
from magickit.web.deploys import router


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    monkeypatch.setattr(records, "default_state_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def client(state_root) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


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


def test_the_page_offers_no_way_to_approve_or_deploy(client, state_root):
    """The unauthenticated surface stays request-and-read only."""
    store = records.DeployStore(state_root)
    request = store.create(target="spirrow-conclair", requested_by="loop", reason="r")
    store.save(request)

    body = client.get("/dashboard/deploys").text

    assert "<form" not in body.lower()
    assert "hx-post" not in body.lower()

    # ...and the router genuinely has no write route to find.
    methods = {method for route in router.routes for method in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD"}
