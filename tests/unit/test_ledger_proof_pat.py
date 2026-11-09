"""The PR-gate ledger proof credential (``MAGICKIT_LEDGER_PROOF_PAT``).

T-pr-review-threads-outlive-their-prs msg-8630 / msg-8632. The no-auth tailnet
instance (:8117) must be able to prove a ledger closable without the ``github``
dispatcher being turned on there. These tests lock in the two halves of that:

- the proof path reaches github-mcp on its own (``_mcp_call`` is a direct httpx
  client, not a lookup in magickit's tool registry), using the proof PAT;
- the proof PAT is not one of the variables that enable the dispatcher, so
  ``install_github_dispatch`` stays a no-op with only it set.

No network: httpx is given a ``MockTransport``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.mcp import github_dispatch, pr_gate_ledger
from magickit.mcp.github_dispatch import (
    _IMPLEMENTER_PAT_ENV,
    _LEGACY_PAT_ENV,
    _REVIEWER_PAT_ENV,
    _any_pat_configured,
    install_github_dispatch,
)
from magickit.mcp.pr_gate_ledger import LEDGER_PROOF_PAT_ENV, PrRef
from magickit.mcp.tools import health as health_tools

_HEAD = "0123456789abcdef0123456789abcdef01234567"
_GH_URL = "http://github-mcp.test:8116/mcp"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (LEDGER_PROOF_PAT_ENV, _IMPLEMENTER_PAT_ENV, _LEGACY_PAT_ENV, _REVIEWER_PAT_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_MCP_URL", _GH_URL)
    return monkeypatch


def _text_result(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


class _FakeGithubMcp:
    """A github-mcp sidecar behind httpx.MockTransport that records requests."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str | None, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append((str(request.url), request.headers.get("authorization"), body))
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 0, "result": {}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        args = body["params"]["arguments"]
        if args["method"] == "get":
            result = _text_result(
                {"merged": True, "state": "closed", "head": {"sha": _HEAD}}
            )
        else:
            result = _text_result([{"id": 42, "state": "APPROVED", "commit_id": _HEAD}])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    def tool_calls(self) -> list[tuple[str, str | None, dict[str, Any]]]:
        return [r for r in self.requests if r[2].get("method") == "tools/call"]


def _patched_async_client(fake: _FakeGithubMcp):
    real = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real(*args, **kwargs)

    return patch.object(github_dispatch.httpx, "AsyncClient", side_effect=_factory)


def _capture_tool_names(install: Any) -> list[str]:
    names: list[str] = []

    def fake_tool(*args: Any, **kwargs: Any):
        def decorator(fn):
            names.append(kwargs.get("name") or fn.__name__)
            return fn

        return decorator

    mcp = MagicMock()
    mcp.tool = fake_tool
    install(mcp)
    return names


# --------------------------------------------------------------------------- #
# the invariant msg-8632 locks in
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_proof_pat_alone_proves_a_ledger_without_the_dispatcher(
    _clean_env: pytest.MonkeyPatch,
) -> None:
    """Only the proof PAT set, dispatcher never installed: the proof still works.

    Both ``pull_request_read`` calls go straight to ``GITHUB_MCP_URL`` with the
    proof PAT as Bearer, and a merged + exact-head APPROVED PR is closable.
    """
    _clean_env.setenv(LEDGER_PROOF_PAT_ENV, "proof-token")
    fake = _FakeGithubMcp()
    with _patched_async_client(fake):
        verdict = await pr_gate_ledger.fetch_ledger_verdict(
            PrRef("SpirrowGames", "spirrow-mindwire", 358)
        )

    assert verdict.closable is True, verdict.reason
    assert verdict.approving_review_id == 42
    calls = fake.tool_calls()
    assert [c[2]["params"]["arguments"]["method"] for c in calls] == ["get", "get_reviews"]
    for url, auth, body in calls:
        assert url == _GH_URL
        assert auth == "Bearer proof-token"
        assert body["params"]["name"] == "pull_request_read"
    # every request of every exchange carried the proof PAT, nothing else
    assert {r[1] for r in fake.requests} == {"Bearer proof-token"}


@pytest.mark.asyncio
async def test_fetch_ledger_verdict_hands_the_proof_pat_to_mcp_call(
    _clean_env: pytest.MonkeyPatch,
) -> None:
    """The resolved proof PAT is the ``pat`` argument of both ``_mcp_call`` calls.

    Pins the wiring at the call site itself (gate round 1-2 asked whether the
    resolved token reaches ``_mcp_call``): ``_mcp_call`` is spied on directly,
    with no httpx in between, and must receive ``pat="proof-token"`` twice.
    """
    _clean_env.setenv(LEDGER_PROOF_PAT_ENV, "proof-token")
    seen: list[dict[str, Any]] = []

    async def _spy(method: str, params: dict[str, Any], pat: str) -> Any:
        seen.append({"method": method, "params": params, "pat": pat})
        # Well-formed payloads (as the sidecar would return them), so the test
        # does not depend on how fetch_ledger_verdict handles missing ones.
        if params["arguments"]["method"] == "get":
            return _text_result({"merged": True, "state": "closed", "head": {"sha": _HEAD}})
        return _text_result([{"id": 42, "state": "APPROVED", "commit_id": _HEAD}])

    with patch.object(github_dispatch, "_mcp_call", new=_spy):
        verdict = await pr_gate_ledger.fetch_ledger_verdict(
            PrRef("SpirrowGames", "spirrow-mindwire", 358)
        )

    assert [c["params"]["arguments"]["method"] for c in seen] == ["get", "get_reviews"]
    assert [c["pat"] for c in seen] == ["proof-token", "proof-token"]
    assert verdict.closable is True


@pytest.mark.asyncio
async def test_then_installing_the_dispatcher_still_registers_nothing(
    _clean_env: pytest.MonkeyPatch,
) -> None:
    """msg-8632 check 2: same environment, the dispatcher stays a no-op."""
    _clean_env.setenv(LEDGER_PROOF_PAT_ENV, "proof-token")
    fake = _FakeGithubMcp()
    with _patched_async_client(fake):
        verdict = await pr_gate_ledger.fetch_ledger_verdict(
            PrRef("SpirrowGames", "spirrow-mindwire", 358)
        )
    assert verdict.closable is True
    assert _any_pat_configured() is False
    names = _capture_tool_names(install_github_dispatch)
    assert "github" not in names
    assert "github_operations" not in names
    assert names == []


def test_dispatcher_still_installs_with_an_ordinary_pat(_clean_env: pytest.MonkeyPatch) -> None:
    """Control for the test above: the capture really sees the dispatcher tools."""
    _clean_env.setenv(_IMPLEMENTER_PAT_ENV, "impl")
    names = _capture_tool_names(install_github_dispatch)
    assert "github" in names
    assert "github_operations" in names


def test_proof_pat_is_never_handed_to_dispatcher_operations(
    _clean_env: pytest.MonkeyPatch,
) -> None:
    """The dispatcher's own PAT resolution ignores the proof PAT entirely."""
    _clean_env.setenv(LEDGER_PROOF_PAT_ENV, "proof-token")
    with pytest.raises(github_dispatch._UpstreamError):
        github_dispatch._pat_for_operation("pull_request_read")
    with pytest.raises(github_dispatch._UpstreamError):
        github_dispatch._resolve_pat(_IMPLEMENTER_PAT_ENV)


@pytest.mark.asyncio
async def test_github_mcp_health_stays_disabled_with_only_the_proof_pat(
    _clean_env: pytest.MonkeyPatch,
) -> None:
    _clean_env.setenv(LEDGER_PROOF_PAT_ENV, "proof-token")
    assert (await github_dispatch.upstream_health())["status"] == "disabled"


# --------------------------------------------------------------------------- #
# precedence and fallback
# --------------------------------------------------------------------------- #


def test_proof_pat_wins_over_the_implementer_pat(_clean_env: pytest.MonkeyPatch) -> None:
    _clean_env.setenv(LEDGER_PROOF_PAT_ENV, "proof-token")
    _clean_env.setenv(_IMPLEMENTER_PAT_ENV, "impl")
    assert pr_gate_ledger._resolve_ledger_proof_pat() == "proof-token"


@pytest.mark.parametrize(
    ("env", "expected"),
    [({_IMPLEMENTER_PAT_ENV: "impl"}, "impl"), ({_LEGACY_PAT_ENV: "legacy"}, "legacy")],
)
def test_without_the_proof_pat_the_old_fallback_is_unchanged(
    _clean_env: pytest.MonkeyPatch, env: dict[str, str], expected: str
) -> None:
    for k, v in env.items():
        _clean_env.setenv(k, v)
    assert pr_gate_ledger._resolve_ledger_proof_pat() == expected


@pytest.mark.asyncio
async def test_no_credential_at_all_is_still_a_refusal() -> None:
    verdict = await pr_gate_ledger.fetch_ledger_verdict(
        PrRef("SpirrowGames", "spirrow-mindwire", 358)
    )
    assert verdict.closable is False
    assert "_UpstreamError" in verdict.reason


# --------------------------------------------------------------------------- #
# the service_health entry
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, {"status": "disabled"}),
        ({LEDGER_PROOF_PAT_ENV: "p"}, {"status": "configured", "source": "proof_pat"}),
        (
            {LEDGER_PROOF_PAT_ENV: "p", _IMPLEMENTER_PAT_ENV: "i"},
            {"status": "configured", "source": "proof_pat"},
        ),
        ({_IMPLEMENTER_PAT_ENV: "i"}, {"status": "configured", "source": "implementer"}),
        ({_LEGACY_PAT_ENV: "l"}, {"status": "configured", "source": "legacy"}),
        ({_REVIEWER_PAT_ENV: "r"}, {"status": "disabled"}),
    ],
)
def test_ledger_proof_status_reports_each_state(
    _clean_env: pytest.MonkeyPatch, env: dict[str, str], expected: dict[str, str]
) -> None:
    for k, v in env.items():
        _clean_env.setenv(k, v)
    status = pr_gate_ledger.ledger_proof_status()
    for k, v in expected.items():
        assert status[k] == v
    # the token value itself is never reported
    for v in env.values():
        assert v not in status.values()


# --------------------------------------------------------------------------- #
# one source for the fallback order (PR #119 gate round 1, advisory 2)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({_IMPLEMENTER_PAT_ENV: "i", _LEGACY_PAT_ENV: "l"}, ("i", _IMPLEMENTER_PAT_ENV)),
        ({_LEGACY_PAT_ENV: "l"}, ("l", _LEGACY_PAT_ENV)),
    ],
)
def test_resolve_pat_with_source_names_the_variable(
    _clean_env: pytest.MonkeyPatch, env: dict[str, str], expected: tuple[str, str]
) -> None:
    for k, v in env.items():
        _clean_env.setenv(k, v)
    assert github_dispatch._resolve_pat_with_source(_IMPLEMENTER_PAT_ENV) == expected
    assert github_dispatch._resolve_pat(_IMPLEMENTER_PAT_ENV) == expected[0]


def test_resolve_pat_with_source_raises_when_unset(_clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(github_dispatch._UpstreamError):
        github_dispatch._resolve_pat_with_source(_IMPLEMENTER_PAT_ENV)


def test_health_follows_an_upstream_fallback_it_does_not_know(
    _clean_env: pytest.MonkeyPatch,
) -> None:
    """If github_dispatch grows a new fallback variable, the health entry and
    the proof read still agree: both come from ``_resolve_pat_with_source``."""

    def _new_fallback(role_env: str) -> tuple[str, str]:
        return "n", "GITHUB_MCP_PAT_SOMETHING_NEW"

    with patch.object(github_dispatch, "_resolve_pat_with_source", _new_fallback):
        assert pr_gate_ledger._resolve_ledger_proof_pat() == "n"
        status = pr_gate_ledger.ledger_proof_status()
    assert status == {"status": "configured", "source": "GITHUB_MCP_PAT_SOMETHING_NEW"}


@pytest.mark.asyncio
async def test_service_health_carries_ledger_proof_outside_the_ratio(
    _clean_env: pytest.MonkeyPatch,
) -> None:
    """The no-auth instance shape: ledger_proof configured, github_mcp disabled."""
    _clean_env.setenv(LEDGER_PROOF_PAT_ENV, "proof-token")
    registered: dict[str, Any] = {}

    def fake_tool(*args: Any, **kwargs: Any):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mcp = MagicMock()
    mcp.tool = fake_tool
    health_tools.register_tools(mcp, Settings())
    healthy = AsyncMock(return_value={"status": "healthy"})
    with (
        patch.object(health_tools, "_check_cognilens", healthy),
        patch.object(health_tools, "_check_prismind", healthy),
        patch.object(health_tools, "_check_lexora", healthy),
        patch.object(health_tools, "_check_conclair", healthy),
    ):
        result = await registered["service_health"]()

    assert result["services"]["ledger_proof"] == {"status": "configured", "source": "proof_pat"}
    assert result["services"]["github_mcp"]["status"] == "disabled"
    assert result["status"] == "healthy"
    assert "proof-token" not in json.dumps(result)
