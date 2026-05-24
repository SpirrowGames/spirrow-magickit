"""Tests for github_dispatch PAT identity routing.

Covers the pure helpers that pick which PAT (implementer vs reviewer) a github
operation runs under, including the legacy single-PAT fallback. No network.
"""

import pytest

from magickit.mcp.github_dispatch import (
    _IMPLEMENTER_PAT_ENV,
    _LEGACY_PAT_ENV,
    _REVIEWER_OPS,
    _REVIEWER_PAT_ENV,
    _any_pat_configured,
    _pat_for_operation,
    _resolve_pat,
    _UpstreamError,
)

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
    """Start each test with no GitHub PAT env vars set."""
    for name in _ALL_PAT_ENVS:
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
