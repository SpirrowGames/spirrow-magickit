"""D-8 (1): ``owner`` existence check on thread open.

spirrow-mindwire T-next-line-carries-who-not-why msg-2144 D-8 (1), sequenced
in T-conclair-persist-disposition-and-d8-readback by Bohr msg-1337 / msg-1341
step 3 (Einstein msg-1342). ``owner`` takes the same existence check as
``next_participant``, through ``_lookup_identity_normalized`` (ADR-11
normalisation before the registry -- Einstein msg-2145 ADVISORY):

- a registered owner opens the thread;
- an owner registered only under its ADR-11 key spelling also opens it;
- an unregistered owner is refused before any write (``OwnerUnknownError``);
- a registry that cannot answer refuses too (fail-closed);
- the browser route takes the same check.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.mcp.tools import chatroom as chatroom_tools

_UNREGISTERED = {"success": True, "found": False, "identity": None, "message": "none"}


def _identity(name: str) -> dict:
    return {
        "success": True,
        "found": True,
        "identity": {
            "identity_name": name,
            "user": "sgadmin",
            "allowed_roles": ["proposer", "integrator"],
            "independence_class": "main-chain",
            "persona_description": "",
        },
        "message": "ok",
    }


def _registry(known: set[str]):
    async def _fn(*, identity_name: str, **_: Any) -> dict:
        return _identity(identity_name) if identity_name in known else _UNREGISTERED

    return AsyncMock(side_effect=_fn)


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


@pytest.fixture
def wired():
    settings = Settings(
        conclair_url="http://localhost:8115",
        conclair_timeout=5.0,
        prismind_url="http://localhost:8002",
        prismind_timeout=5.0,
    )
    chat = MagicMock()
    chat.open_thread = AsyncMock(return_value={"thread": {}, "msg": {}})
    chat.close = AsyncMock()
    prismind = MagicMock()
    prismind.get_identity = _registry({"Bohr", "operator"})
    tools = _capture_tools(settings)
    with (
        patch.object(chatroom_tools, "_adapter", return_value=chat),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
    ):
        yield tools, chat, prismind


async def _open(tools, owner: str) -> dict:
    return await tools["chatroom_open_thread"](
        project="p", thread_id="T-1", title="t", owner=owner, propose_content="hi"
    )


@pytest.mark.parametrize("owner", ["Bohr", "operator"])
@pytest.mark.asyncio
async def test_registered_owner_opens(wired, owner: str) -> None:
    tools, chat, _ = wired
    result = await _open(tools, owner)
    assert "error_type" not in result
    assert chat.open_thread.call_args.kwargs["owner"] == owner


@pytest.mark.asyncio
async def test_owner_registered_under_adr11_key_opens(wired) -> None:
    tools, chat, prismind = wired
    result = await _open(tools, "Operator")
    assert "error_type" not in result
    names = [c.kwargs["identity_name"] for c in prismind.get_identity.await_args_list]
    assert names[:2] == ["Operator", "operator"]
    # Recorded as given; normalisation is for the lookup only.
    assert chat.open_thread.call_args.kwargs["owner"] == "Operator"


@pytest.mark.parametrize("owner", ["claude.ai", "main", "operator-lane", "Bhor"])
@pytest.mark.asyncio
async def test_unregistered_owner_is_refused_before_any_write(wired, owner: str) -> None:
    tools, chat, _ = wired
    result = await _open(tools, owner)
    assert result["error_type"] == "OwnerUnknownError"
    assert result["details"] == {"owner": owner}
    chat.open_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_registry_outage_refuses(wired) -> None:
    tools, chat, prismind = wired
    prismind.get_identity = AsyncMock(side_effect=RuntimeError("prismind down"))
    result = await _open(tools, "Bohr")
    assert result["error_type"] == "OwnerValidationUnavailableError"
    assert result["details"]["reason"] == "prismind down"
    chat.open_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_blank_owner_is_left_to_conclair(wired) -> None:
    tools, chat, prismind = wired
    await _open(tools, "")
    prismind.get_identity.assert_not_awaited()
    chat.open_thread.assert_awaited_once()


# ---- browser route --------------------------------------------------------


@pytest.mark.asyncio
async def test_ui_open_thread_takes_the_owner_check() -> None:
    """The browser must not be the way around D-8 (1)."""
    from magickit.main import create_app

    chatroom_tools.configure(
        Settings(conclair_url="http://localhost:8115", prismind_url="http://localhost:8002")
    )
    adapter = AsyncMock()
    prismind = AsyncMock()
    prismind.get_identity = _registry(set())
    try:
        with (
            patch.object(chatroom_tools, "_adapter", return_value=adapter),
            patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
        ):
            app = create_app()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.post(
                    "/ui/projects/p/threads",
                    data={
                        "thread_id": "T-1",
                        "title": "t",
                        "owner": "ghost",
                        "propose_content": "c",
                    },
                )
    finally:
        chatroom_tools._settings = None
    assert "OwnerUnknownError" in response.text
    adapter.open_thread.assert_not_called()
