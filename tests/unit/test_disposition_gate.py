"""``disposition`` on chatroom posts (T-magickit-stop-disposition-intake).

Spec: Bohr DESIGN v6 §2 範囲 1 (schema), v8 §2 (input table and DoD),
endorsed by Einstein msg-1007. Covered here:

- a post without ``disposition`` is accepted exactly as before, and Prismind
  is not consulted;
- the schema ``{kind: done} | {kind: blocked_on, trigger: {arm, ref}, wake}``
  is accepted and forwarded; anything else is refused before the write;
- an agent naming ``human`` as trigger arm or wake is refused;
- an author whose registered roles include ``human`` is exempt;
- the exemption is decided by role and NOT by ``independence_class``;
- ``wake`` must be a registered identity, checked as given and by its ADR-11
  key (DESIGN v11 §1); ``none`` is refused only because it is not registered.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.config import Settings
from magickit.mcp.tools import chatroom as chatroom_tools

_UNREGISTERED = {"success": True, "found": False, "identity": None, "message": "none"}


def _identity(name: str, *, roles: list[str], independence_class: str = "cooperative") -> dict:
    return {
        "success": True,
        "found": True,
        "identity": {
            "identity_name": name,
            "user": "sgadmin",
            "allowed_roles": roles,
            "independence_class": independence_class,
            "persona_description": "",
        },
        "message": "ok",
    }


def _capture_tools(settings: Settings) -> dict[str, Any]:
    registered: dict[str, Any] = {}

    def fake_tool(*args: Any, **kwargs: Any):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp = MagicMock()
    mock_mcp.tool = fake_tool
    chatroom_tools.register_tools(mock_mcp, settings)
    return registered


_REGISTRY = {
    "Heisenberg": _identity("Heisenberg", roles=["implementer"]),
    "human": _identity("human", roles=["human"]),
    "Takahito": _identity("Takahito", roles=["human"]),
    "pr-gate-relay": _identity("pr-gate-relay", roles=["integrator"]),
}


def _registry(extra: dict[str, dict] | None = None):
    table = {**_REGISTRY, **(extra or {})}

    async def _fn(*, identity_name: str, **_: Any) -> dict:
        return table.get(identity_name, _UNREGISTERED)

    return _fn


@pytest.fixture
def wired():
    settings = Settings(
        conclair_url="http://localhost:8115",
        conclair_timeout=5.0,
        prismind_url="http://localhost:8002",
        prismind_timeout=5.0,
    )
    chat = MagicMock()
    chat.post_message = AsyncMock(return_value={"msg": {}, "thread_status_changed_to": None})
    chat.close = AsyncMock()
    prismind = MagicMock()
    prismind.get_identity = AsyncMock(side_effect=_registry())
    tools = _capture_tools(settings)
    with (
        patch.object(chatroom_tools, "_adapter", return_value=chat),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
    ):
        yield tools, chat, prismind


async def _post(tools, *, author: str = "Heisenberg", disposition: Any = None) -> dict:
    args: dict[str, Any] = dict(
        project="p", thread_id="T-1", msg_type="report", author=author, content="c"
    )
    if disposition is not None:
        args["disposition"] = disposition
    return await tools["chatroom_post_message"](**args)


def _blocked(arm: str = "thread", ref: str = "T-other (merged)", wake: str = "Heisenberg") -> dict:
    return {"kind": "blocked_on", "trigger": {"arm": arm, "ref": ref}, "wake": wake}


# ---- backward compatibility ------------------------------------------


@pytest.mark.asyncio
async def test_post_without_disposition_is_unchanged(wired) -> None:
    tools, chat, prismind = wired
    result = await _post(tools)
    assert "error_type" not in result
    assert chat.post_message.call_args.kwargs["disposition"] is None
    prismind.get_identity.assert_not_awaited()


# ---- accepted shapes ---------------------------------------------------


@pytest.mark.asyncio
async def test_done_is_forwarded(wired) -> None:
    tools, chat, prismind = wired
    result = await _post(tools, disposition={"kind": "done"})
    assert "error_type" not in result
    assert chat.post_message.call_args.kwargs["disposition"] == {"kind": "done"}
    prismind.get_identity.assert_not_awaited()


@pytest.mark.parametrize("arm", ["thread", "pr", "deploy", "queue-empty"])
@pytest.mark.asyncio
async def test_agent_arms_are_forwarded_after_wake_lookup_only(wired, arm: str) -> None:
    tools, chat, prismind = wired
    result = await _post(tools, disposition=_blocked(arm=arm))
    assert "error_type" not in result
    assert chat.post_message.call_args.kwargs["disposition"] == _blocked(arm=arm)
    # Only the wake is looked up; the author is not, since no human is named.
    looked_up = [c.kwargs["identity_name"] for c in prismind.get_identity.await_args_list]
    assert looked_up == ["Heisenberg"]


# ---- schema refusals ---------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "done",
        {},
        {"kind": "malformed"},
        {"kind": "done", "extra": 1},
        {"kind": "blocked_on"},
        {"kind": "blocked_on", "trigger": {"arm": "thread", "ref": "x"}},
        {"kind": "blocked_on", "trigger": {"arm": "thread"}, "wake": "Bohr"},
        {"kind": "blocked_on", "trigger": {"arm": "email", "ref": "x"}, "wake": "Bohr"},
        {"kind": "blocked_on", "trigger": {"arm": "thread", "ref": " "}, "wake": "Bohr"},
        {"kind": "blocked_on", "trigger": {"arm": "thread", "ref": "x", "y": 1}, "wake": "Bohr"},
        {"kind": "blocked_on", "trigger": "thread:x", "wake": "Bohr"},
    ],
)
@pytest.mark.asyncio
async def test_invalid_disposition_is_refused_before_write(wired, bad: Any) -> None:
    tools, chat, _ = wired
    result = await _post(tools, disposition=bad)
    assert result["error_type"] == "DispositionInvalidError"
    chat.post_message.assert_not_awaited()


# ---- the human rule ------------------------------------------------------


@pytest.mark.parametrize(
    "disposition",
    [
        _blocked(arm="human", ref="decision"),
        _blocked(wake="human"),
        _blocked(wake=" Human "),  # ADR-11 normalisation: no spelling bypass
    ],
)
@pytest.mark.asyncio
async def test_agent_naming_human_is_refused(wired, disposition: dict) -> None:
    tools, chat, _ = wired
    result = await _post(tools, disposition=disposition)
    assert result["error_type"] == "DispositionHumanNotAllowedError"
    chat.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_unregistered_author_naming_human_is_refused(wired) -> None:
    tools, chat, _ = wired
    result = await _post(tools, author="someone", disposition=_blocked(arm="human", ref="x"))
    assert result["error_type"] == "DispositionHumanNotAllowedError"
    chat.post_message.assert_not_awaited()


@pytest.mark.parametrize(
    "disposition", [_blocked(arm="human", ref="decision"), _blocked(wake="human")]
)
@pytest.mark.asyncio
async def test_role_human_author_is_exempt(wired, disposition: dict) -> None:
    tools, chat, _ = wired
    result = await _post(tools, author="Takahito", disposition=disposition)
    assert "error_type" not in result
    assert chat.post_message.call_args.kwargs["disposition"] == disposition


@pytest.mark.parametrize("independence_class", ["human", "independent", "cooperative", "machine"])
@pytest.mark.asyncio
async def test_exemption_ignores_independence_class(wired, independence_class: str) -> None:
    """DESIGN v7 §2: the key is role, never independence_class (ADR-15 made it a
    gradation). Same verdict for every class: role decides, alone."""
    tools, _, prismind = wired
    disposition = _blocked(arm="human", ref="decision")

    prismind.get_identity = AsyncMock(side_effect=_registry({
        "A": _identity("A", roles=["implementer"], independence_class=independence_class),
        "H": _identity("H", roles=["human"], independence_class=independence_class),
    }))
    refused = await _post(tools, author="A", disposition=disposition)
    assert refused["error_type"] == "DispositionHumanNotAllowedError"

    accepted = await _post(tools, author="H", disposition=disposition)
    assert "error_type" not in accepted


@pytest.mark.asyncio
async def test_registry_outage_fails_closed_for_blocked_on_only(wired) -> None:
    tools, chat, prismind = wired
    prismind.get_identity = AsyncMock(side_effect=RuntimeError("prismind down"))

    for disposition in (_blocked(arm="human", ref="x"), _blocked()):
        refused = await _post(tools, disposition=disposition)
        assert refused["error_type"] == "DispositionValidationUnavailableError"
        assert refused["details"]["reason"] == "prismind down"
    chat.post_message.assert_not_awaited()

    # {kind: done} and no disposition never consult the registry.
    assert "error_type" not in await _post(tools, disposition={"kind": "done"})
    assert "error_type" not in await _post(tools)


# ---- wake existence (DESIGN v11 §1) ---------------------------------------


@pytest.mark.parametrize("wake", ["Nobody", "none", "Einstien"])
@pytest.mark.asyncio
async def test_unregistered_wake_is_refused(wired, wake: str) -> None:
    tools, chat, _ = wired
    result = await _post(tools, disposition=_blocked(wake=wake))
    assert result["error_type"] == "DispositionWakeUnknownError"
    assert result["details"]["wake"] == wake
    chat.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_wake_is_matched_by_its_adr11_key(wired) -> None:
    tools, chat, prismind = wired
    result = await _post(tools, disposition=_blocked(wake="PR_Gate_Relay"))
    assert "error_type" not in result
    looked_up = [c.kwargs["identity_name"] for c in prismind.get_identity.await_args_list]
    assert looked_up == ["PR_Gate_Relay", "pr-gate-relay"]


@pytest.mark.asyncio
async def test_registered_wake_spelling_is_looked_up_once(wired) -> None:
    tools, _, prismind = wired
    await _post(tools, disposition=_blocked(wake="Heisenberg"))
    assert prismind.get_identity.await_count == 1


@pytest.mark.asyncio
async def test_adapter_forwards_disposition_only_when_set() -> None:
    from magickit.adapters.chatroom import ChatroomAdapter

    adapter = ChatroomAdapter.__new__(ChatroomAdapter)
    adapter._request_json = AsyncMock(return_value={})  # type: ignore[method-assign]
    await adapter.post_message(project="p", thread_id="T", type="report", author="a", content="c")
    assert "disposition" not in adapter._request_json.call_args.kwargs["json"]
    await adapter.post_message(
        project="p", thread_id="T", type="report", author="a", content="c",
        disposition={"kind": "done"},
    )
    assert adapter._request_json.call_args.kwargs["json"]["disposition"] == {"kind": "done"}
