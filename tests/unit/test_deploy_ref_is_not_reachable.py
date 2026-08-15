"""R-1: the loop cannot ask for a ref, because there is nowhere to put one.

The rule is "deploy `origin/main`", and the cheapest way to hold a rule
like that is to give the caller no way to express anything else. Not a
validator that compares the ref against an allowed set -- a *missing
parameter*. A validator can be relaxed by someone who reads it as a
list to extend; an absent parameter has to be added back, which shows
up in review as what it is.

So these tests are shaped around absence rather than rejection. They ask
the request path "is there any argument here that could carry a git
ref?" and fail if the answer is yes, whatever that argument is called.
The forbidden-name set is deliberately broader than the obvious `ref`:
the failure this guards against is someone adding `branch=` or
`commit=` under deadline, not someone adding a parameter literally
named `ref` after reading this file.

The one legitimate way a non-default ref enters the system is the human
override of R-1, and it enters through *approval*, not through the
request -- so it is reachable only by whoever can approve, and only with
a reason that lands in the audit log. The last tests here pin that
asymmetry, because it is the whole design: the loop can ask for a
deploy, and cannot say what to deploy.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

from magickit.config import Settings
from magickit.deploy import registry
from magickit.mcp.tools import deploy as deploy_tools

#: Anything a git ref could plausibly be called. The point is to catch a
#: future parameter that means "which code", not to match one spelling.
FORBIDDEN_REF_PARAMS = frozenset(
    {
        "ref",
        "refs",
        "branch",
        "branches",
        "sha",
        "commit",
        "commitish",
        "revision",
        "rev",
        "tag",
        "treeish",
        "tree_ish",
        "head",
        "source_ref",
        "target_ref",
        "git_ref",
        "version",
    }
)


def _capture_tools(settings: Settings, *, allow_approval: bool) -> dict[str, Any]:
    """Register the deploy tools and capture them by name.

    Intercepts the ``@mcp.tool()`` decorator rather than reading
    FastMCP's registry, matching tests/unit/test_mcp_chatroom_tools.py --
    FastMCP's lookup API has moved across 2.x minors.
    """
    registered: dict[str, Any] = {}

    def fake_tool(*args: Any, **kwargs: Any):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp = MagicMock()
    mock_mcp.tool = fake_tool
    deploy_tools.register_tools(mock_mcp, settings, allow_approval=allow_approval)
    return registered


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def loop_tools(settings: Settings) -> dict[str, Any]:
    """The tool surface as the loop sees it: no approval tool."""
    return _capture_tools(settings, allow_approval=False)


@pytest.fixture
def human_tools(settings: Settings) -> dict[str, Any]:
    """The tool surface behind authentication: approval included."""
    return _capture_tools(settings, allow_approval=True)


# ── the request path carries no ref ──────────────────────────────


def test_deploy_request_has_no_parameter_that_could_carry_a_ref(loop_tools):
    params = set(inspect.signature(loop_tools["deploy_request"]).parameters)
    offending = params & FORBIDDEN_REF_PARAMS
    assert not offending, (
        f"deploy_request grew a ref-shaped parameter: {sorted(offending)}. "
        "R-1 holds because the caller cannot name a ref, not because a "
        "validator rejects the ones it dislikes."
    )


def test_deploy_request_takes_no_var_kwargs(loop_tools):
    """``**kwargs`` would re-open the door this test closes."""
    kinds = [p.kind for p in inspect.signature(loop_tools["deploy_request"]).parameters.values()]
    assert inspect.Parameter.VAR_KEYWORD not in kinds
    assert inspect.Parameter.VAR_POSITIONAL not in kinds


def test_the_rollback_path_cannot_carry_a_ref_either(loop_tools):
    """Rollback is the second way code other than `origin/main` goes
    live, so it gets the same treatment: the caller names a past deploy
    and the commit is read out of magickit's record of it.
    """
    params = set(inspect.signature(loop_tools["deploy_rollback"]).parameters)

    assert not params & FORBIDDEN_REF_PARAMS
    assert params == {"request_id", "requested_by", "reason"}


def test_the_ref_is_a_module_constant_not_an_argument():
    """One constant, per R-1's "1 行で済むから"."""
    assert registry.DEPLOY_REF == "origin/main"


def test_creating_a_request_record_cannot_set_an_override(loop_tools):
    """The record type has an override field; the request path cannot fill it."""
    from magickit.deploy.records import DeployRequest

    fields = set(inspect.signature(DeployRequest).parameters)
    assert "override_ref" in fields, "the human override has to be expressible somewhere"

    # ...but not from the requesting side.
    request_params = set(inspect.signature(loop_tools["deploy_request"]).parameters)
    assert "override_ref" not in request_params
    assert "override_reason" not in request_params


# ── the override exists, on the approval side, with a reason ──────


def test_override_is_reachable_only_from_the_approval_tool(human_tools, loop_tools):
    approve_params = set(inspect.signature(human_tools["deploy_approve"]).parameters)
    assert "override_ref" in approve_params
    assert "override_reason" in approve_params

    # And the approval tool is not part of the surface the loop gets at
    # all -- so neither is the override.
    assert "deploy_approve" not in loop_tools


async def test_override_without_a_reason_is_refused(human_tools, tmp_path, monkeypatch):
    """R-1: a different branch is allowed *if* the reason is recorded."""
    from magickit.deploy import records

    monkeypatch.setattr(records, "default_state_root", lambda: tmp_path)

    created = await human_tools["deploy_request"](
        target="spirrow-conclair",
        requested_by="mindwire-conductor",
        reason="conclair#10 merged but not live",
    )
    assert created["ok"] is True

    refused = await human_tools["deploy_approve"](
        request_id=created["request_id"],
        approved_by="Takahito",
        override_ref="feat/something",
        override_reason="",
    )
    assert refused["ok"] is False
    assert refused["error_type"] == "override_reason_required"
