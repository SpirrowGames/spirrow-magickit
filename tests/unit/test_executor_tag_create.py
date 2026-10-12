"""ADR-2026-10-09-23 D10: the one path that creates ``executor/vX.Y.Z`` tags.

Two halves:

- INV-0 (OFF): with the default settings nothing is registered and no
  GitHub call can happen. The executor tools enter the MCP server only
  through ``magickit.mcp.tools.executor.register_tools``, so "that call
  registers nothing" is "the server's tool list is what it was before".
- ON: every refusal happens before anything is written, an existing tag
  is never re-pointed, and the write order is tag object, then ref.

GitHub is replaced by :class:`FakeApi`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from magickit.config import Settings
from magickit.deploy import executor_tags
from magickit.deploy.executor_tags import ApiResponse, ExecutorTagError
from magickit.mcp.tools import executor as executor_tools

REPO = "SpirrowGames/spirrow-mindwire"
SHA = "a" * 40
TAG_OBJ = "b" * 40
ROOT = Path(__file__).resolve().parents[2]


class FakeApi:
    def __init__(
        self,
        *,
        ref_status: int = 404,
        compare_status: int = 200,
        compare_body: Any = "default",
        tag_status: int = 201,
        ref_create_status: int = 201,
    ) -> None:
        self.calls: list[tuple] = []
        self.entered = 0
        self.exited = 0
        self.ref_status = ref_status
        self.compare_status = compare_status
        self.compare_body = {"status": "ahead"} if compare_body == "default" else compare_body
        self.tag_status = tag_status
        self.ref_create_status = ref_create_status

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, *exc):
        self.exited += 1

    @property
    def writes(self) -> list[tuple]:
        return [c for c in self.calls if c[0] in ("create_tag_object", "create_ref")]

    async def get_ref(self, repo, ref):
        self.calls.append(("get_ref", repo, ref))
        return ApiResponse(self.ref_status, {"ref": ref} if self.ref_status == 200 else None)

    async def compare(self, repo, base, head):
        self.calls.append(("compare", repo, base, head))
        return ApiResponse(self.compare_status, self.compare_body)

    async def create_tag_object(self, repo, tag, message, sha):
        self.calls.append(("create_tag_object", repo, tag, message, sha))
        return ApiResponse(self.tag_status, {"sha": TAG_OBJ} if self.tag_status == 201 else None)

    async def create_ref(self, repo, ref, sha):
        self.calls.append(("create_ref", repo, ref, sha))
        return ApiResponse(self.ref_create_status, None)


def _create(api: FakeApi, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "repo": REPO,
        "version": "1.2.3",
        "sha": SHA,
        "message": "executor 1.2.3",
        "requested_by": "Heisenberg",
    }
    kwargs.update(overrides)
    return asyncio.run(executor_tags.create_executor_tag(api, **kwargs))


def _refusal(api: FakeApi, **overrides: Any) -> ExecutorTagError:
    with pytest.raises(ExecutorTagError) as info:
        _create(api, **overrides)
    return info.value


def _capture(settings: Settings) -> tuple[dict[str, Any], MagicMock]:
    registered: dict[str, Any] = {}

    def fake_tool(*args: Any, **kwargs: Any):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp = MagicMock()
    mock_mcp.tool = MagicMock(side_effect=fake_tool)
    executor_tools.register_tools(mock_mcp, settings)
    return registered, mock_mcp


# --- INV-0: setting OFF ----------------------------------------------------


def test_setting_defaults_to_off():
    assert Settings().executor_pool_enabled is False


def test_off_registers_no_tool(monkeypatch):
    monkeypatch.delenv("MAGICKIT_EXECUTOR_POOL_ENABLED", raising=False)
    registered, mock_mcp = _capture(Settings())
    assert registered == {}
    mock_mcp.tool.assert_not_called()
    assert mock_mcp.mock_calls == []


def test_off_makes_no_github_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("GitHub must not be reached when the pool is off")

    monkeypatch.setattr(executor_tools, "_make_api", boom)
    monkeypatch.setattr(executor_tags, "HttpxTagApi", boom)
    registered, _ = _capture(Settings(executor_pool_enabled=False))
    assert registered == {}


def test_server_wires_executor_tools_only_through_register_tools():
    """The OFF tests above stand for the whole server only if this module is
    the one door the executor tools come through."""
    source = (ROOT / "src/magickit/mcp_server.py").read_text(encoding="utf-8")
    assert "executor.register_tools(mcp, settings)" in source
    assert "executor_tag_create" not in source


# --- ON: registration ------------------------------------------------------


def test_on_registers_executor_tag_create():
    registered, _ = _capture(Settings(executor_pool_enabled=True))
    assert set(registered) == {"executor_tag_create"}


def test_tool_reports_refusal_as_error_dict(monkeypatch):
    api = FakeApi(ref_status=200)
    monkeypatch.setattr(executor_tools, "_make_api", lambda pat_env: api)
    registered, _ = _capture(Settings(executor_pool_enabled=True))
    out = asyncio.run(
        registered["executor_tag_create"]("1.2.3", SHA, "m", "Heisenberg")
    )
    assert out["ok"] is False and out["error_type"] == "tag_exists"
    assert api.writes == []


def test_tool_without_pat_is_not_configured(monkeypatch):
    for name in ("GITHUB_MCP_PAT_IMPLEMENTER", "GITHUB_MCP_PAT"):
        monkeypatch.delenv(name, raising=False)
    registered, _ = _capture(Settings(executor_pool_enabled=True))
    out = asyncio.run(registered["executor_tag_create"]("1.2.3", SHA, "m", "x"))
    assert out == {"ok": False, "error_type": "not_configured", "message": out["message"]}


def test_tool_targets_configured_repo(monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(executor_tools, "_make_api", lambda pat_env: api)
    registered, _ = _capture(
        Settings(executor_pool_enabled=True, executor_tag_repo="o/r")
    )
    out = asyncio.run(registered["executor_tag_create"]("0.1.0", SHA, "m", "x"))
    assert out["ok"] is True and out["repo"] == "o/r"
    assert {c[1] for c in api.calls} == {"o/r"}


# --- ON: success -----------------------------------------------------------


@pytest.mark.parametrize("status", ["ahead", "identical"])
def test_creates_tag_object_then_ref(status):
    api = FakeApi(compare_body={"status": status})
    out = _create(api)
    assert out == {
        "ok": True,
        "repo": REPO,
        "tag": "executor/v1.2.3",
        "ref": "refs/tags/executor/v1.2.3",
        "commit": SHA,
        "tag_object": TAG_OBJ,
        "requested_by": "Heisenberg",
    }
    assert [c[0] for c in api.calls] == ["get_ref", "compare", "create_tag_object", "create_ref"]
    assert api.calls[0] == ("get_ref", REPO, "tags/executor/v1.2.3")
    assert api.calls[1] == ("compare", REPO, SHA, "main")
    _, _, tag, message, obj = api.calls[2]
    assert (tag, obj) == ("executor/v1.2.3", SHA)
    assert "Requested-by: Heisenberg" in message
    # The ref points at the annotated tag object, not at the commit.
    assert api.calls[3] == ("create_ref", REPO, "refs/tags/executor/v1.2.3", TAG_OBJ)


def test_zero_version_is_valid():
    assert _create(FakeApi(), version="0.0.0")["tag"] == "executor/v0.0.0"


# --- ON: refusals (nothing written) ----------------------------------------


@pytest.mark.parametrize(
    "version",
    [
        "v1.2.3",
        "1.2",
        "1.2.3.4",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3-rc.1",
        "1.2.3+build",
        " 1.2.3",
        "1.2.3\n",
        "1.2.3\r\n",
        "",
        "a.b.c",
        None,
    ],
)
def test_invalid_version_refused_before_any_call(version):
    api = FakeApi()
    err = _refusal(api, version=version)
    assert err.error_type == "invalid_version"
    assert api.calls == []


@pytest.mark.parametrize(
    "sha", ["abc1234", "a" * 40 + "\n", "A" * 40, "g" * 40, "a" * 39, "a" * 41, "", None]
)
def test_invalid_sha_refused_before_any_call(sha):
    api = FakeApi()
    assert _refusal(api, sha=sha).error_type == "invalid_sha"
    assert api.calls == []


@pytest.mark.parametrize("field", ["message", "requested_by"])
@pytest.mark.parametrize("value", ["", "   ", None])
def test_empty_text_refused_before_any_call(field, value):
    api = FakeApi()
    assert _refusal(api, **{field: value}).error_type == "invalid_argument"
    assert api.calls == []


@pytest.mark.parametrize("repo", ["", "noslash", "a/b/c", None])
def test_invalid_repo_refused_before_any_call(repo):
    api = FakeApi()
    assert _refusal(api, repo=repo).error_type == "invalid_argument"
    assert api.calls == []


def test_existing_tag_refused_not_repointed():
    api = FakeApi(ref_status=200)
    assert _refusal(api).error_type == "tag_exists"
    assert api.writes == []


@pytest.mark.parametrize("status", ["behind", "diverged"])
def test_sha_not_reachable_from_main_refused(status):
    api = FakeApi(compare_body={"status": status})
    assert _refusal(api).error_type == "sha_not_on_main"
    assert api.writes == []


def test_unknown_sha_refused():
    api = FakeApi(compare_status=404, compare_body={})
    assert _refusal(api).error_type == "sha_not_on_main"
    assert api.writes == []


@pytest.mark.parametrize("body", [{}, None, {"status": None}])
def test_unreadable_compare_refused(body):
    api = FakeApi(compare_body=body)
    assert _refusal(api).error_type == "sha_not_on_main"
    assert api.writes == []


def test_ref_lookup_failure_refused():
    api = FakeApi(ref_status=500)
    assert _refusal(api).error_type == "github_error"
    assert api.calls == [("get_ref", REPO, "tags/executor/v1.2.3")]


def test_compare_failure_refused():
    api = FakeApi(compare_status=500)
    assert _refusal(api).error_type == "github_error"
    assert api.writes == []


def test_tag_object_failure_creates_no_ref():
    api = FakeApi(tag_status=403)
    assert _refusal(api).error_type == "github_error"
    assert [c[0] for c in api.writes] == ["create_tag_object"]


def test_concurrent_creation_reported_as_tag_exists():
    api = FakeApi(ref_create_status=422)
    assert _refusal(api).error_type == "tag_exists"


def test_ref_creation_failure_refused():
    api = FakeApi(ref_create_status=403)
    assert _refusal(api).error_type == "github_error"


# --- ruleset JSON (placed, not applied) -------------------------------------


def _ruleset(name: str) -> dict[str, Any]:
    return json.loads((ROOT / "config/github" / name).read_text(encoding="utf-8"))


def test_immutable_ruleset_has_no_bypass():
    rs = _ruleset("executor-tags-immutable.ruleset.json")
    assert rs["target"] == "tag" and rs["enforcement"] == "active"
    assert rs["conditions"]["ref_name"]["include"] == ["refs/tags/executor/*"]
    # non_fast_forward is a branch rule; on a tag, moving the ref is `update`.
    assert {r["type"] for r in rs["rules"]} == {"deletion", "update"}
    assert rs["bypass_actors"] == []


def test_create_ruleset_restricts_creation_with_one_bypass_actor():
    rs = _ruleset("executor-tags-create.ruleset.json")
    assert rs["target"] == "tag" and rs["enforcement"] == "active"
    assert rs["conditions"]["ref_name"]["include"] == ["refs/tags/executor/*"]
    assert [r["type"] for r in rs["rules"]] == ["creation"]
    assert len(rs["bypass_actors"]) == 1


def test_pat_env_comes_from_settings(monkeypatch):
    seen: list[str] = []
    api = FakeApi()

    def make(pat_env):
        seen.append(pat_env)
        return api

    monkeypatch.setattr(executor_tools, "_make_api", make)
    registered, _ = _capture(
        Settings(executor_pool_enabled=True, executor_tag_pat_env="MAGICKIT_TAGGER_PAT")
    )
    out = asyncio.run(registered["executor_tag_create"]("1.0.0", SHA, "m", "x"))
    assert out["ok"] is True
    assert seen == ["MAGICKIT_TAGGER_PAT"]


def test_pat_env_default_and_unset_token_is_not_configured(monkeypatch):
    assert Settings().executor_tag_pat_env == "GITHUB_MCP_PAT_IMPLEMENTER"
    monkeypatch.delenv("MAGICKIT_TAGGER_PAT", raising=False)
    monkeypatch.delenv("GITHUB_MCP_PAT", raising=False)
    registered, _ = _capture(
        Settings(executor_pool_enabled=True, executor_tag_pat_env="MAGICKIT_TAGGER_PAT")
    )
    out = asyncio.run(registered["executor_tag_create"]("1.0.0", SHA, "m", "x"))
    assert out["error_type"] == "not_configured"


def test_tool_runs_inside_one_api_session(monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(executor_tools, "_make_api", lambda pat_env: api)
    registered, _ = _capture(Settings(executor_pool_enabled=True))
    out = asyncio.run(registered["executor_tag_create"]("1.0.0", SHA, "m", "x"))
    assert out["ok"] is True
    assert (api.entered, api.exited) == (1, 1)


def test_httpx_api_uses_one_client_for_all_calls():
    """PR-gate advisory @ 9391dd8: the four calls of one run share a client."""
    import httpx

    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers["Authorization"]))
        path = request.url.path
        if "/git/ref/" in path:
            return httpx.Response(404, json={})
        if "/compare/" in path:
            return httpx.Response(200, json={"status": "ahead"})
        if path.endswith("/git/tags"):
            return httpx.Response(201, json={"sha": TAG_OBJ})
        return httpx.Response(201, json={"ref": "x"})

    api = executor_tags.HttpxTagApi("tok", transport=httpx.MockTransport(handler))
    clients: list[Any] = []

    async def run():
        async with api:
            clients.append(api._client)
            out = await executor_tags.create_executor_tag(
                api, repo=REPO, version="1.2.3", sha=SHA, message="m", requested_by="x"
            )
            clients.append(api._client)
        return out

    out = asyncio.run(run())
    assert out["ok"] is True
    assert clients[0] is clients[1] and clients[0] is not None
    assert api._client is None  # closed on exit
    assert [m for m, _, _ in seen] == ["GET", "GET", "POST", "POST"]
    assert seen[0][1] == f"/repos/{REPO}/git/ref/tags/executor/v1.2.3"
    assert seen[1][1] == f"/repos/{REPO}/compare/{SHA}...main"
    assert {a for _, _, a in seen} == {"Bearer tok"}


def test_httpx_api_refuses_use_outside_session():
    api = executor_tags.HttpxTagApi("tok")
    with pytest.raises(RuntimeError):
        asyncio.run(api.get_ref(REPO, "tags/executor/v1.0.0"))
