"""One identity-existence lookup, and the D-8 (2) owner audit.

Spec: T-conclair-persist-disposition-and-d8-readback, Bohr msg-1300 §2 (B-1 +
(2)) as amended by msg-1302 §2, endorsed by Einstein. spirrow-mindwire
T-next-line-carries-who-not-why msg-2144 D-8 (2) is the requirement the audit
implements.

- ``_lookup_identity_normalized`` is the single lookup every existence check
  goes through: the name as given, then (on a confirmed miss only) its ADR-11
  key. Prismind matches names verbatim and stores display names, so a
  key-only lookup would refuse every registered identity (msg-1302 §2).
- ``next_participant`` now uses it too, so acceptance only widens.
- ``chatroom_check_integrity`` appends an ``owner_unresolved_identity`` issue
  per thread whose owner does not resolve; an owner the registry could not
  answer for is reported apart, not counted.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.mcp.tools import chatroom as chatroom_tools

PROJECT = "p"


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


def _registered(name: str, roles: list[str] | None = None) -> dict:
    return {
        "success": True,
        "found": True,
        "identity": {
            "identity_name": name,
            "user": "sgadmin",
            "allowed_roles": roles if roles is not None else [],
            "independence_class": "cooperative",
            "persona_description": "",
        },
        "message": "ok",
    }


_UNREGISTERED = {"success": True, "found": False, "identity": None, "message": "none"}
_OUTAGE = {"success": False, "found": False, "identity": None, "message": "down"}


def _registry(mapping: dict[str, dict]):
    async def _fn(*, identity_name: str, **_: Any) -> dict:
        return mapping.get(identity_name, _UNREGISTERED)

    return _fn


def _looked_up(prismind: MagicMock) -> list[str]:
    return [c.kwargs["identity_name"] for c in prismind.get_identity.await_args_list]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        conclair_url="http://localhost:8115",
        conclair_timeout=5.0,
        prismind_url="http://localhost:8002",
        prismind_timeout=5.0,
    )


@pytest.fixture
def chat() -> MagicMock:
    adapter = MagicMock()
    adapter.open_thread = AsyncMock(return_value={"thread": {}, "msg": {}})
    adapter.post_message = AsyncMock(
        return_value={"msg": {}, "thread_status_changed_to": None}
    )
    adapter.get_thread = AsyncMock(
        return_value={"thread": {"tags": []}, "messages": [], "mode": "full"}
    )
    adapter.check_integrity = AsyncMock(
        return_value={"issues": [], "issue_count": 0, "checked_at": "2026-10-21T00:00:00Z"}
    )
    adapter.list_threads = AsyncMock(
        return_value={"items": [], "total": 0, "limit": 1000, "offset": 0}
    )
    adapter.close = AsyncMock()
    return adapter


@pytest.fixture
def prismind() -> MagicMock:
    adapter = MagicMock()
    adapter.get_identity = AsyncMock(
        side_effect=_registry({
            "Heisenberg": _registered("Heisenberg"),
            "Bohr": _registered("Bohr"),
            "claude-code": _registered("claude-code"),
        })
    )
    return adapter


@pytest.fixture
def wired(settings: Settings, chat: MagicMock, prismind: MagicMock):
    tools = _capture_tools(settings)
    with (
        patch.object(chatroom_tools, "_adapter", return_value=chat),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
    ):
        yield tools, chat, prismind


# ---- the shared lookup ------------------------------------------------


@pytest.mark.asyncio
async def test_registered_spelling_is_one_lookup(wired) -> None:
    _, _, prismind = wired
    lookup = await chatroom_tools._lookup_identity_normalized("Heisenberg")
    assert lookup.found
    assert _looked_up(prismind) == ["Heisenberg"]


@pytest.mark.asyncio
async def test_miss_retries_by_adr11_key(wired) -> None:
    _, _, prismind = wired
    lookup = await chatroom_tools._lookup_identity_normalized("Claude_Code")
    assert lookup.found
    assert _looked_up(prismind) == ["Claude_Code", "claude-code"]


@pytest.mark.asyncio
async def test_miss_whose_key_is_the_same_spelling_is_one_lookup(wired) -> None:
    _, _, prismind = wired
    lookup = await chatroom_tools._lookup_identity_normalized("nobody")
    assert not lookup.is_unavailable and not lookup.found
    assert _looked_up(prismind) == ["nobody"]


@pytest.mark.asyncio
async def test_outage_is_returned_without_a_second_lookup(wired) -> None:
    _, _, prismind = wired
    prismind.get_identity = AsyncMock(return_value=_OUTAGE)
    lookup = await chatroom_tools._lookup_identity_normalized("Claude_Code")
    assert lookup.is_unavailable
    assert prismind.get_identity.await_count == 1


def test_wake_and_next_participant_share_the_lookup() -> None:
    """Only one existence lookup remains; the wake-only copy is gone."""
    assert not hasattr(chatroom_tools, "_lookup_wake")


# ---- display-name identities pass every existence gate ---------------


@pytest.mark.asyncio
async def test_display_name_is_accepted_as_next_participant(wired) -> None:
    tools, chat, _ = wired
    result = await tools["chatroom_post_message"](
        project=PROJECT, thread_id="T-1", msg_type="handoff", author="Bohr",
        content="c", embodiment="web_ai_chat", next_participant="Heisenberg",
    )
    assert "error_type" not in result
    assert chat.post_message.call_args.kwargs["next_participant"] == "Heisenberg"


@pytest.mark.asyncio
async def test_display_name_is_accepted_as_wake(wired) -> None:
    tools, chat, _ = wired
    result = await tools["chatroom_post_message"](
        project=PROJECT, thread_id="T-1", msg_type="report", author="Bohr",
        content="c",
        disposition={"kind": "blocked_on", "trigger": {"arm": "pr", "ref": "o/r#1"},
                     "wake": "Heisenberg"},
    )
    assert "error_type" not in result
    chat.post_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_display_name_owner_is_not_an_audit_issue(wired) -> None:
    tools, chat, _ = wired
    chat.list_threads = AsyncMock(return_value={
        "items": [{"thread_id": "T-1", "owner": "Heisenberg"}], "total": 1,
    })
    report = await tools["chatroom_check_integrity"](project=PROJECT)
    assert report["issues"] == []
    assert report["owner_audit"]["status"] == "complete"


@pytest.mark.asyncio
async def test_next_participant_acceptance_only_widens(wired) -> None:
    """An ADR-11 spelling of a registered key now resolves; an unknown name
    is still refused, and an outage still fails closed."""
    tools, chat, prismind = wired
    ok = await tools["chatroom_post_message"](
        project=PROJECT, thread_id="T-1", msg_type="handoff", author="Bohr",
        content="c", embodiment="web_ai_chat", next_participant="Claude_Code",
    )
    assert "error_type" not in ok
    # Forwarded verbatim: the lookup normalises, the stored value does not change.
    assert chat.post_message.call_args.kwargs["next_participant"] == "Claude_Code"

    unknown = await tools["chatroom_post_message"](
        project=PROJECT, thread_id="T-1", msg_type="handoff", author="Bohr",
        content="c", embodiment="web_ai_chat", next_participant="Einstien",
    )
    assert unknown["error_type"] == "NextParticipantUnknownError"

    prismind.get_identity = AsyncMock(return_value=_OUTAGE)
    down = await tools["chatroom_post_message"](
        project=PROJECT, thread_id="T-1", msg_type="handoff", author="Bohr",
        content="c", embodiment="web_ai_chat", next_participant="Heisenberg",
    )
    assert down["error_type"] == "NextParticipantValidationUnavailableError"


# ---- D-8 (2): owner audit ---------------------------------------------


@pytest.mark.asyncio
async def test_unresolved_owners_are_appended_to_conclair_issues(wired) -> None:
    tools, chat, _ = wired
    conclair_issue = {"type": "orphan_message", "thread_id": "T-0", "msg_id": "msg-1",
                      "details": "x"}
    chat.check_integrity = AsyncMock(return_value={
        "issues": [conclair_issue], "issue_count": 1, "checked_at": "t",
    })
    chat.list_threads = AsyncMock(return_value={
        "items": [
            {"thread_id": "T-1", "owner": "Bohr"},
            {"thread_id": "T-2", "owner": "sweeper-bot"},
            {"thread_id": "T-3", "owner": ""},
        ],
        "total": 3,
    })

    report = await tools["chatroom_check_integrity"](project=PROJECT)

    assert report["issues"][0] == conclair_issue
    added = report["issues"][1:]
    assert [(i["type"], i["thread_id"]) for i in added] == [
        ("owner_unresolved_identity", "T-2"),
        ("owner_unresolved_identity", "T-3"),
    ]
    assert all(i["msg_id"] is None and i["details"] for i in added)
    assert report["issue_count"] == 3
    assert report["checked_at"] == "t"
    audit = report["owner_audit"]
    assert audit["status"] == "complete"
    assert audit["threads_checked"] == 3
    assert audit["unresolved"] == 2
    assert audit["unverified"] == []


@pytest.mark.asyncio
async def test_each_owner_is_looked_up_once_per_call(wired) -> None:
    tools, chat, prismind = wired
    chat.list_threads = AsyncMock(return_value={
        "items": [{"thread_id": f"T-{i}", "owner": o}
                  for i, o in enumerate(["Bohr", "ghost", "Bohr", "ghost", "Bohr"])],
        "total": 5,
    })
    report = await tools["chatroom_check_integrity"](project=PROJECT)
    assert _looked_up(prismind).count("Bohr") == 1
    assert _looked_up(prismind).count("ghost") == 1
    assert report["owner_audit"]["owners_checked"] == 2
    assert report["issue_count"] == 2


@pytest.mark.asyncio
async def test_outage_owner_is_reported_apart_not_counted(wired) -> None:
    tools, chat, prismind = wired

    async def _fn(*, identity_name: str, **_: Any) -> dict:
        if identity_name == "Flaky":
            return _OUTAGE
        return _registered(identity_name)

    prismind.get_identity = AsyncMock(side_effect=_fn)
    chat.list_threads = AsyncMock(return_value={
        "items": [{"thread_id": "T-1", "owner": "Flaky"},
                  {"thread_id": "T-2", "owner": "Bohr"}],
        "total": 2,
    })
    report = await tools["chatroom_check_integrity"](project=PROJECT)
    assert "error_type" not in report
    assert report["issues"] == []
    assert report["issue_count"] == 0
    unverified = report["owner_audit"]["unverified"]
    assert [(u["thread_id"], u["owner"]) for u in unverified] == [("T-1", "Flaky")]
    assert unverified[0]["reason"]


@pytest.mark.asyncio
async def test_all_pages_are_audited(wired) -> None:
    tools, chat, _ = wired
    page1 = {"items": [{"thread_id": f"T-{i}", "owner": "Bohr"} for i in range(1000)],
             "total": 1001}
    page2 = {"items": [{"thread_id": "T-last", "owner": "ghost"}], "total": 1001}
    chat.list_threads = AsyncMock(side_effect=[page1, page2])
    report = await tools["chatroom_check_integrity"](project=PROJECT)
    offsets = [c.kwargs["offset"] for c in chat.list_threads.await_args_list]
    assert offsets == [0, 1000]
    assert report["owner_audit"]["threads_checked"] == 1001
    assert [i["thread_id"] for i in report["issues"]] == ["T-last"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    {"error_type": "ConclairUpstreamError", "error": "boom"},
    {"unexpected": True},
])
async def test_listing_failure_marks_audit_unavailable(wired, failure: dict) -> None:
    tools, chat, prismind = wired
    chat.list_threads = AsyncMock(return_value=failure)
    report = await tools["chatroom_check_integrity"](project=PROJECT)
    assert report["issue_count"] == 0
    assert report["owner_audit"]["status"] == "unavailable"
    assert report["owner_audit"]["reason"]
    prismind.get_identity.assert_not_awaited()


@pytest.mark.asyncio
async def test_listing_transport_error_marks_audit_unavailable(wired) -> None:
    tools, chat, _ = wired
    chat.list_threads = AsyncMock(side_effect=httpx.ConnectError("refused"))
    report = await tools["chatroom_check_integrity"](project=PROJECT)
    assert report["owner_audit"] == {"status": "unavailable", "reason": "refused"}
    chat.close.assert_awaited()


@pytest.mark.asyncio
async def test_conclair_error_envelope_is_passed_through_unaudited(wired) -> None:
    tools, chat, prismind = wired
    envelope = {"error_type": "ProjectNotFound", "error": "no such project"}
    chat.check_integrity = AsyncMock(return_value=envelope)
    report = await tools["chatroom_check_integrity"](project=PROJECT)
    assert report == envelope
    chat.list_threads.assert_not_awaited()
    prismind.get_identity.assert_not_awaited()
