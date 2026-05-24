"""Tests for github_dispatch PAT identity routing and merge policy.

Covers the pure helpers that pick which PAT (implementer vs reviewer) a github
operation runs under (incl. legacy single-PAT fallback), and the merge guard
that refuses ``merge_pull_request`` into protected branches. No network: the
upstream ``_mcp_call`` is mocked.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from magickit.mcp.github_dispatch import (
    _IMPLEMENTER_PAT_ENV,
    _LEGACY_PAT_ENV,
    _PROTECTED_BASE_ENV,
    _REVIEWER_OPS,
    _REVIEWER_PAT_ENV,
    _any_pat_configured,
    _execute_operation,
    _merge_block_reason,
    _pat_for_operation,
    _pr_base_ref,
    _protected_base_branches,
    _resolve_pat,
    _UpstreamError,
)


def _pr_read_result(base_ref: str) -> dict:
    """Shape returned by upstream pull_request_read(get): PR JSON in content text."""
    return {"content": [{"text": json.dumps({"number": 1, "base": {"ref": base_ref}})}]}


def _make_mcp(base_ref: str = "develop") -> AsyncMock:
    """Mock _mcp_call: pull_request_read -> base_ref, merge_pull_request -> MERGED."""

    def _call(method, params, pat):
        name = params.get("name")
        if name == "pull_request_read":
            return _pr_read_result(base_ref)
        if name == "merge_pull_request":
            return {"content": [{"text": "MERGED"}]}
        return {"content": []}

    return AsyncMock(side_effect=_call)


def _called_op_names(mock: AsyncMock) -> list[str]:
    """github op names forwarded to the mocked _mcp_call."""
    return [c.args[1].get("name") for c in mock.call_args_list]

# A sampling of contents/PR-writing operations that must use the implementer.
_IMPLEMENTER_OPS = [
    "create_or_update_file",
    "delete_file",
    "push_files",
    "create_branch",
    "create_pull_request",
    "merge_pull_request",
    "get_file_contents",  # reads default to implementer too
    "pull_request_read",
]

_ALL_PAT_ENVS = (_LEGACY_PAT_ENV, _IMPLEMENTER_PAT_ENV, _REVIEWER_PAT_ENV)


@pytest.fixture(autouse=True)
def _clear_pat_env(monkeypatch):
    """Start each test with no GitHub PAT / merge-policy env vars set."""
    for name in (*_ALL_PAT_ENVS, _PROTECTED_BASE_ENV):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class TestAnyPatConfigured:
    def test_none_set(self):
        assert _any_pat_configured() is False

    @pytest.mark.parametrize("env", _ALL_PAT_ENVS)
    def test_single_set(self, monkeypatch, env):
        monkeypatch.setenv(env, "tok")
        assert _any_pat_configured() is True


class TestResolvePat:
    def test_role_specific_preferred(self, monkeypatch):
        monkeypatch.setenv(_IMPLEMENTER_PAT_ENV, "impl-tok")
        monkeypatch.setenv(_LEGACY_PAT_ENV, "legacy-tok")
        assert _resolve_pat(_IMPLEMENTER_PAT_ENV) == "impl-tok"

    def test_falls_back_to_legacy(self, monkeypatch):
        monkeypatch.setenv(_LEGACY_PAT_ENV, "legacy-tok")
        assert _resolve_pat(_REVIEWER_PAT_ENV) == "legacy-tok"

    def test_raises_when_neither_set(self):
        with pytest.raises(_UpstreamError):
            _resolve_pat(_IMPLEMENTER_PAT_ENV)


class TestPatForOperation:
    @pytest.mark.parametrize("op", sorted(_REVIEWER_OPS))
    def test_review_ops_use_reviewer(self, monkeypatch, op):
        monkeypatch.setenv(_IMPLEMENTER_PAT_ENV, "impl-tok")
        monkeypatch.setenv(_REVIEWER_PAT_ENV, "rev-tok")
        assert _pat_for_operation(op) == "rev-tok"

    @pytest.mark.parametrize("op", _IMPLEMENTER_OPS)
    def test_other_ops_use_implementer(self, monkeypatch, op):
        monkeypatch.setenv(_IMPLEMENTER_PAT_ENV, "impl-tok")
        monkeypatch.setenv(_REVIEWER_PAT_ENV, "rev-tok")
        assert _pat_for_operation(op) == "impl-tok"

    def test_legacy_only_routes_both_to_legacy(self, monkeypatch):
        """A single legacy PAT keeps pre-split behaviour: every op uses it."""
        monkeypatch.setenv(_LEGACY_PAT_ENV, "legacy-tok")
        assert _pat_for_operation("create_pull_request") == "legacy-tok"
        assert _pat_for_operation("pull_request_review_write") == "legacy-tok"

    def test_review_op_without_reviewer_pat_falls_back(self, monkeypatch):
        """Reviewer op with only an implementer+legacy env falls back to legacy."""
        monkeypatch.setenv(_IMPLEMENTER_PAT_ENV, "impl-tok")
        monkeypatch.setenv(_LEGACY_PAT_ENV, "legacy-tok")
        assert _pat_for_operation("pull_request_review_write") == "legacy-tok"


class TestProtectedBaseBranches:
    def test_default_is_main(self):
        assert _protected_base_branches() == frozenset({"main"})

    def test_env_override_comma_separated(self, monkeypatch):
        monkeypatch.setenv(_PROTECTED_BASE_ENV, "main, release ,prod")
        assert _protected_base_branches() == frozenset({"main", "release", "prod"})


class TestMergeBlockReason:
    def test_unprotected_base_allowed(self):
        assert _merge_block_reason("develop") is None

    def test_protected_base_blocked(self):
        assert _merge_block_reason("main") is not None

    def test_unknown_base_fails_closed(self):
        assert _merge_block_reason(None) is not None

    def test_custom_protected_set(self, monkeypatch):
        monkeypatch.setenv(_PROTECTED_BASE_ENV, "release")
        assert _merge_block_reason("release") is not None
        assert _merge_block_reason("main") is None  # no longer protected


class TestPrBaseRef:
    async def test_returns_base_ref(self):
        with patch("magickit.mcp.github_dispatch._mcp_call", _make_mcp("develop")):
            ref = await _pr_base_ref({"owner": "o", "repo": "r", "pullNumber": 1}, "pat")
        assert ref == "develop"

    async def test_missing_args_returns_none_without_call(self):
        mock = AsyncMock()
        with patch("magickit.mcp.github_dispatch._mcp_call", mock):
            assert await _pr_base_ref({"owner": "o"}, "pat") is None
        mock.assert_not_called()

    async def test_upstream_error_returns_none(self):
        mock = AsyncMock(side_effect=_UpstreamError("boom"))
        with patch("magickit.mcp.github_dispatch._mcp_call", mock):
            assert await _pr_base_ref({"owner": "o", "repo": "r", "pullNumber": 1}, "p") is None

    async def test_unparseable_body_returns_none(self):
        mock = AsyncMock(return_value={"content": [{"text": "not-json"}]})
        with patch("magickit.mcp.github_dispatch._mcp_call", mock):
            assert await _pr_base_ref({"owner": "o", "repo": "r", "pullNumber": 1}, "p") is None


class TestExecuteOperation:
    """Identity + merge policy on the full dispatch path (upstream mocked)."""

    _MERGE_ARGS = {"owner": "o", "repo": "r", "pullNumber": 1}

    async def test_merge_into_develop_is_forwarded(self, monkeypatch):
        monkeypatch.setenv(_IMPLEMENTER_PAT_ENV, "impl")
        mock = _make_mcp(base_ref="develop")
        with patch("magickit.mcp.github_dispatch._mcp_call", mock):
            res = await _execute_operation("merge_pull_request", self._MERGE_ARGS)
        assert res == {"content": [{"text": "MERGED"}]}
        assert "merge_pull_request" in _called_op_names(mock)  # actually merged

    async def test_merge_into_main_is_blocked_and_not_forwarded(self, monkeypatch):
        monkeypatch.setenv(_IMPLEMENTER_PAT_ENV, "impl")
        mock = _make_mcp(base_ref="main")
        with patch("magickit.mcp.github_dispatch._mcp_call", mock):
            res = await _execute_operation("merge_pull_request", self._MERGE_ARGS)
        assert res["blocked_by"] == "policy"
        assert res["base_ref"] == "main"
        names = _called_op_names(mock)
        assert "pull_request_read" in names  # base was looked up
        assert "merge_pull_request" not in names  # merge never reached upstream

    async def test_merge_blocked_when_base_unknown(self, monkeypatch):
        """Upstream lookup failure => fail closed, merge not forwarded."""
        monkeypatch.setenv(_IMPLEMENTER_PAT_ENV, "impl")
        mock = AsyncMock(side_effect=_UpstreamError("lookup down"))
        with patch("magickit.mcp.github_dispatch._mcp_call", mock):
            res = await _execute_operation("merge_pull_request", self._MERGE_ARGS)
        assert res["blocked_by"] == "policy"
        assert "merge_pull_request" not in _called_op_names(mock)

    async def test_custom_protected_branch_blocks_develop(self, monkeypatch):
        monkeypatch.setenv(_IMPLEMENTER_PAT_ENV, "impl")
        monkeypatch.setenv(_PROTECTED_BASE_ENV, "develop")
        mock = _make_mcp(base_ref="develop")
        with patch("magickit.mcp.github_dispatch._mcp_call", mock):
            res = await _execute_operation("merge_pull_request", self._MERGE_ARGS)
        assert res["blocked_by"] == "policy"

    async def test_non_merge_op_forwarded_without_lookup(self, monkeypatch):
        monkeypatch.setenv(_IMPLEMENTER_PAT_ENV, "impl")
        mock = _make_mcp()
        with patch("magickit.mcp.github_dispatch._mcp_call", mock):
            await _execute_operation("get_file_contents", {"owner": "o", "repo": "r"})
        assert _called_op_names(mock) == ["get_file_contents"]  # no pull_request_read
