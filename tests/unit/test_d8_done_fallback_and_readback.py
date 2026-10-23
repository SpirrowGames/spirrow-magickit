"""D-8 (3) owner fallback and C-1 disposition read-back on chatroom posts.

T-conclair-persist-disposition-and-d8-readback, Bohr msg-1337 / msg-1341
(Einstein msg-1342). Covered here:

- (3) a non-closing ``done`` on a thread whose owner is not a registered
  identity is stored as ``blocked_on(human)`` with a ``fallback`` whose keys
  are exactly Conclair ``DispositionFallback`` (reason / owner / declared);
- (3) an owner that resolves (as given, or by its ADR-11 key) passes the
  ``done`` through untouched;
- (3) a registry outage, or a thread that cannot be read, does NOT rewrite:
  the ``done`` is stored as sent and the response says ``unverified``;
- (3) a closing post is never rewritten, and nothing is auto-closed;
- C-1 a stored value that differs from what was sent adds ``mismatch``; a
  read-back that cannot be done adds ``unverified``; the post never becomes
  an error and a match adds nothing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.config import Settings
from magickit.mcp.disposition import owner_unresolved_fallback, parse_disposition
from magickit.mcp.tools import chatroom as chatroom_tools

_UNREGISTERED = {"success": True, "found": False, "identity": None, "message": "none"}


def _identity(name: str, roles: list[str]) -> dict:
    return {
        "success": True,
        "found": True,
        "identity": {
            "identity_name": name,
            "user": "sgadmin",
            "allowed_roles": roles,
            "independence_class": "cooperative",
            "persona_description": "",
        },
        "message": "ok",
    }


_REGISTRY = {
    "Heisenberg": _identity("Heisenberg", ["implementer"]),
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


class _Conclair:
    """Stores posts; the stored disposition is ``store_as(sent)`` (verbatim by default)."""

    def __init__(self, owner: Any) -> None:
        self.owner = owner
        self.stored: list[dict[str, Any]] = []
        self.store_as = lambda sent: sent
        self.thread_read_error: dict[str, Any] | None = None
        self.full_read_error: dict[str, Any] | Exception | None = None
        self.drop_msg_id = False
        self.mock = MagicMock()
        self.mock.post_message = AsyncMock(side_effect=self._post)
        self.mock.get_thread = AsyncMock(side_effect=self._get_thread)
        self.mock.close = AsyncMock()

    async def _post(self, **kwargs: Any) -> dict:
        msg = {
            "msg_id": f"msg-{len(self.stored) + 1}",
            "disposition": self.store_as(kwargs.get("disposition")),
        }
        self.stored.append(msg)
        out = {} if self.drop_msg_id else {"msg_id": msg["msg_id"]}
        return {"msg": out, "thread_status_changed_to": None}

    async def _get_thread(self, *, mode: str = "full", **_: Any) -> dict:
        if mode == "summary" and self.thread_read_error is not None:
            return self.thread_read_error
        if mode == "full" and self.full_read_error is not None:
            if isinstance(self.full_read_error, Exception):
                raise self.full_read_error
            return self.full_read_error
        return {"thread": {"owner": self.owner}, "messages": [dict(m) for m in self.stored]}


@pytest.fixture
def make():
    settings = Settings(
        conclair_url="http://localhost:8115",
        conclair_timeout=5.0,
        prismind_url="http://localhost:8002",
        prismind_timeout=5.0,
    )
    tools = _capture_tools(settings)
    patches: list[Any] = []

    def _make(owner: Any = "Heisenberg", registry: Any = None):
        conclair = _Conclair(owner)
        prismind = MagicMock()

        async def _lookup(*, identity_name: str, **_: Any) -> dict:
            return _REGISTRY.get(identity_name, _UNREGISTERED)

        prismind.get_identity = registry if registry is not None else AsyncMock(side_effect=_lookup)
        for p in (
            patch.object(chatroom_tools, "_adapter", return_value=conclair.mock),
            patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
        ):
            p.start()
            patches.append(p)
        return tools, conclair, prismind

    yield _make
    for p in patches:
        p.stop()


_BLOCKED = {"kind": "blocked_on", "trigger": {"arm": "pr", "ref": "o/r#1"}, "wake": "Heisenberg"}


async def _post(tools, *, disposition: Any, msg_type: str = "report", **extra: Any) -> dict:
    return await tools["chatroom_post_message"](
        project="p",
        thread_id="T-1",
        msg_type=msg_type,
        author="Heisenberg",
        content="c",
        disposition=disposition,
        **extra,
    )


def _sent(conclair: _Conclair) -> Any:
    return conclair.mock.post_message.call_args.kwargs["disposition"]


# ---- (3) the fallback shape ---------------------------------------------


def test_fallback_shape_is_conclairs_disposition_fallback() -> None:
    wire = owner_unresolved_fallback(owner="operator-lane", thread_id="T-9")
    assert wire == {
        "kind": "blocked_on",
        "trigger": {"arm": "human", "ref": "T-9"},
        "wake": "human",
        "fallback": {"reason": "owner_unresolved", "owner": "operator-lane", "declared": "done"},
    }
    # spirrow-conclair DispositionFallback @ 77a5aa7 forbids extras: exactly
    # these three keys.
    assert set(wire["fallback"]) == {"reason", "owner", "declared"}


def test_caller_cannot_send_a_fallback() -> None:
    forged = owner_unresolved_fallback(owner="x", thread_id="T-1")
    with pytest.raises(ValueError, match="unexpected keys"):
        parse_disposition(forged)


# ---- (3) rewrite / pass-through ------------------------------------------


@pytest.mark.asyncio
async def test_done_on_unresolved_owner_is_parked_for_human(make) -> None:
    tools, conclair, _ = make(owner="operator-lane")
    result = await _post(tools, disposition={"kind": "done"})
    assert "error_type" not in result
    expected = owner_unresolved_fallback(owner="operator-lane", thread_id="T-1")
    assert _sent(conclair) == expected
    check = result["disposition_owner_check"]
    assert check["status"] == "rewritten"
    assert check["owner"] == "operator-lane"
    assert check["declared"] == {"kind": "done"}
    assert check["stored_as"] == expected
    # Read-back matched the rewritten value: no readback note.
    assert "disposition_readback" not in result
    # Not a close.
    assert conclair.mock.post_message.call_args.kwargs["closes_thread"] is None


@pytest.mark.asyncio
async def test_done_on_resolving_owner_passes_through(make) -> None:
    tools, conclair, _ = make(owner="Heisenberg")
    result = await _post(tools, disposition={"kind": "done"})
    assert _sent(conclair) == {"kind": "done"}
    assert "disposition_owner_check" not in result


@pytest.mark.asyncio
async def test_owner_resolving_by_adr11_key_passes_through(make) -> None:
    async def registry(*, identity_name: str, **_: Any) -> dict:
        # The registry knows only the key spelling.
        if identity_name == "pr-gate-relay":
            return _identity("pr-gate-relay", [])
        return _UNREGISTERED

    tools, conclair, prismind = make(owner="PR_Gate_Relay", registry=AsyncMock(side_effect=registry))
    result = await _post(tools, disposition={"kind": "done"})
    assert _sent(conclair) == {"kind": "done"}
    assert "disposition_owner_check" not in result
    names = [c.kwargs["identity_name"] for c in prismind.get_identity.await_args_list]
    assert names == ["PR_Gate_Relay", "pr-gate-relay"]


@pytest.mark.asyncio
async def test_registry_outage_does_not_rewrite(make) -> None:
    tools, conclair, _ = make(
        owner="operator-lane", registry=AsyncMock(side_effect=RuntimeError("prismind down"))
    )
    result = await _post(tools, disposition={"kind": "done"})
    assert "error_type" not in result
    assert _sent(conclair) == {"kind": "done"}
    assert result["disposition_owner_check"] == {
        "status": "unverified",
        "owner": "operator-lane",
        "reason": "prismind down",
    }


@pytest.mark.asyncio
async def test_unreadable_thread_does_not_rewrite(make) -> None:
    tools, conclair, prismind = make(owner="operator-lane")
    conclair.thread_read_error = {"error_type": "ChatroomUnavailableError", "error": "x"}
    result = await _post(tools, disposition={"kind": "done"})
    assert _sent(conclair) == {"kind": "done"}
    assert result["disposition_owner_check"]["status"] == "unverified"
    prismind.get_identity.assert_not_awaited()


@pytest.mark.parametrize("owner", [None, "", "  "])
@pytest.mark.asyncio
async def test_missing_owner_is_unverified_not_rewritten(make, owner: Any) -> None:
    tools, conclair, _ = make(owner=owner)
    result = await _post(tools, disposition={"kind": "done"})
    assert _sent(conclair) == {"kind": "done"}
    assert result["disposition_owner_check"]["status"] == "unverified"


@pytest.mark.asyncio
async def test_blocked_on_is_never_rewritten(make) -> None:
    tools, conclair, _ = make(owner="operator-lane")
    result = await _post(tools, disposition=_BLOCKED)
    assert _sent(conclair) == _BLOCKED
    assert "disposition_owner_check" not in result


@pytest.mark.asyncio
async def test_no_disposition_reads_nothing(make) -> None:
    tools, conclair, prismind = make(owner="operator-lane")
    result = await tools["chatroom_post_message"](
        project="p", thread_id="T-1", msg_type="report", author="Heisenberg", content="c"
    )
    assert "disposition_owner_check" not in result
    assert "disposition_readback" not in result
    conclair.mock.get_thread.assert_not_awaited()
    prismind.get_identity.assert_not_awaited()


@pytest.mark.asyncio
async def test_closing_done_is_not_rewritten(make) -> None:
    tools, conclair, _ = make(owner="operator-lane")
    proceed = {
        "action": "proceed",
        "content": "c",
        "owner_override": False,
        "owner_override_reason": None,
        "close_sanction": None,
    }
    with (
        patch.object(chatroom_tools, "_enforce_close_policies", AsyncMock(return_value=proceed)),
        patch.object(
            chatroom_tools,
            "_check_close_permitted",
            AsyncMock(return_value=MagicMock(error=None, role=None)),
        ),
    ):
        result = await _post(
            tools,
            disposition={"kind": "done"},
            msg_type="decide",
            closes_thread="T-1",
            embodiment="claude-code",
        )
    assert "error_type" not in result
    assert conclair.mock.post_message.call_args.kwargs["closes_thread"] == "T-1"
    assert _sent(conclair) == {"kind": "done"}
    assert "disposition_owner_check" not in result


# ---- C-1 read-back --------------------------------------------------------


@pytest.mark.asyncio
async def test_readback_mismatch_is_reported_not_raised(make) -> None:
    tools, conclair, _ = make()
    conclair.store_as = lambda sent: None  # a Conclair that drops the field
    result = await _post(tools, disposition={"kind": "done"})
    assert "error_type" not in result
    assert result["disposition_readback"] == {
        "status": "mismatch",
        "sent": {"kind": "done"},
        "stored": None,
    }


@pytest.mark.asyncio
async def test_readback_after_rewrite_compares_the_rewritten_value(make) -> None:
    tools, conclair, _ = make(owner="operator-lane")
    conclair.store_as = lambda sent: {"kind": "done"}
    result = await _post(tools, disposition={"kind": "done"})
    rb = result["disposition_readback"]
    assert rb["status"] == "mismatch"
    assert rb["sent"] == owner_unresolved_fallback(owner="operator-lane", thread_id="T-1")
    assert rb["stored"] == {"kind": "done"}


@pytest.mark.parametrize(
    "error",
    [{"error_type": "ChatroomUnavailableError", "error": "down"}, RuntimeError("boom")],
)
@pytest.mark.asyncio
async def test_readback_unavailable_is_unverified(make, error: Any) -> None:
    tools, conclair, _ = make()
    conclair.full_read_error = error
    result = await _post(tools, disposition=_BLOCKED)
    assert "error_type" not in result
    assert result["disposition_readback"]["status"] == "unverified"
    assert result["disposition_readback"]["sent"] == _BLOCKED


@pytest.mark.asyncio
async def test_readback_without_msg_id_is_unverified(make) -> None:
    tools, conclair, _ = make()
    conclair.drop_msg_id = True
    result = await _post(tools, disposition={"kind": "done"})
    assert result["disposition_readback"]["status"] == "unverified"


@pytest.mark.asyncio
async def test_failed_post_is_returned_without_readback(make) -> None:
    tools, conclair, _ = make()
    conclair.mock.post_message = AsyncMock(
        return_value={"error_type": "ChatroomValidationError", "error": "422"}
    )
    result = await _post(tools, disposition={"kind": "done"})
    assert result == {"error_type": "ChatroomValidationError", "error": "422"}
