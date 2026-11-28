"""Role ``product_owner`` and the role ``human`` reservation (ADR-2026-11-28-25).

Thread: spirrow-magickit T-magickit-product-owner-role, design v3 (Bohr
msg-1801 / msg-1803, approved by Einstein in reply to msg-1803). Each test names the design item
it pins: D(a) .. D(h).

What is deliberately *not* pinned: that a caller declaring ``author="human"``
gets the embodiment exemption. It does today, because the exemption key is the
caller-declared author string, and ADR-2026-11-28-25 D-5 (server-resolved
identity only) is unmet in Magickit and was returned as such (msg-1801 §A).
Fixing that as specified behaviour would harden the gap.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.main import create_app
from magickit.mcp import settled_disposition as sd
from magickit.mcp.tools import chatroom as chatroom_tools
from magickit.mcp.tools import session as session_tools
from magickit.web import chatroom_proxy
from tests.unit.test_settled_disposition_close import (
    _adapter as _settled_adapter,
)
from tests.unit.test_settled_disposition_close import (
    _close as _settled_close,
)
from tests.unit.test_settled_disposition_close import (
    _reason as _settled_reason,
)
from tests.unit.test_settled_disposition_close import (
    _settings as _settled_settings,
)
from tests.unit.test_settled_disposition_close import (
    _thread as _settled_thread,
)

PROJECT = "p"
THREAD = "T-1"
STATE_TRANSITIONS = ("handoff", "ack", "decide")


def _record(
    allowed_roles: list[str], *, independence_class: str | None = "main-chain"
) -> dict[str, Any]:
    identity: dict[str, Any] = {"allowed_roles": allowed_roles}
    if independence_class is not None:
        identity["independence_class"] = independence_class
    return {"success": True, "found": True, "identity": identity, "message": "ok"}


def _registry(table: dict[str, Any]) -> MagicMock:
    """Per-name Prismind answers; an absent name is "not registered"."""

    async def get_identity(*, identity_name: str) -> Any:
        answer = table.get(identity_name)
        if answer is None:
            return {"success": True, "found": False, "identity": None, "message": "none"}
        return answer

    prismind = MagicMock()
    prismind.get_identity = AsyncMock(side_effect=get_identity)
    return prismind


def _capture(register, settings: Settings) -> dict[str, Any]:
    registered: dict[str, Any] = {}

    def fake_tool(*_args: Any, **_kwargs: Any):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mcp = MagicMock()
    mcp.tool = fake_tool
    register(mcp, settings)
    return registered


def _conclair() -> MagicMock:
    adapter = MagicMock()
    adapter.post_message = AsyncMock(
        return_value={"msg": {"msg_id": "msg-9"}, "thread_status_changed_to": None}
    )
    adapter.close_thread = AsyncMock(return_value={"thread": {}, "decide_msg": {}})
    adapter.get_thread = AsyncMock(
        return_value={"thread": {"tags": [], "owner": "Bohr"}, "messages": [], "mode": "full"}
    )
    adapter.close = AsyncMock()
    return adapter


@pytest.fixture
def settings() -> Settings:
    return Settings(
        conclair_url="http://localhost:8115",
        prismind_url="http://localhost:8002",
    )


@pytest.fixture(autouse=True)
def _configured(settings: Settings):
    """The web routes read module-level settings bound at app startup."""
    chatroom_tools.configure(settings)
    yield
    chatroom_tools._settings = None


async def _mcp_write(
    settings: Settings,
    prismind: MagicMock,
    *,
    path: str,
    msg_type: str,
    author: str,
    role: str = "",
    embodiment: str = "",
) -> tuple[dict[str, Any], MagicMock]:
    """One MCP write: ``path`` is "post" or "close"."""
    tools = _capture(chatroom_tools.register_tools, settings)
    conclair = _conclair()
    with (
        patch.object(chatroom_tools, "_adapter", return_value=conclair),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
    ):
        if path == "close":
            result = await tools["chatroom_close_thread"](
                project=PROJECT, thread_id=THREAD, summary_content="done",
                author=author, role=role, embodiment=embodiment,
            )
        else:
            result = await tools["chatroom_post_message"](
                project=PROJECT, thread_id=THREAD, msg_type=msg_type,
                author=author, content="c", role=role, embodiment=embodiment,
            )
    return result, conclair


async def _web_post(
    prismind: MagicMock, data: dict[str, str]
) -> tuple[httpx.Response, MagicMock]:
    conclair = _conclair()
    with (
        patch.object(chatroom_tools, "_adapter", return_value=conclair),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                f"/ui/projects/{PROJECT}/threads/{THREAD}/messages", data=data
            )
    return response, conclair


# --- D(a): the identity ``human`` needs no embodiment --------------------


@pytest.mark.asyncio
async def test_a_human_decide_without_embodiment_passes_over_mcp(settings) -> None:
    result, conclair = await _mcp_write(
        settings, _registry({}), path="post", msg_type="decide", author="human"
    )
    assert "error_type" not in result
    conclair.post_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_human_decide_without_embodiment_passes_over_the_web() -> None:
    response, conclair = await _web_post(
        _registry({}), {"type": "decide", "author": "human", "content": "c"}
    )
    assert "EmbodimentRequiredError" not in response.text
    conclair.post_message.assert_awaited_once()


# --- D(b): Fermi is not exempt -------------------------------------------


@pytest.mark.parametrize("msg_type", STATE_TRANSITIONS)
@pytest.mark.asyncio
async def test_fermi_needs_embodiment_for_state_transitions(settings, msg_type) -> None:
    prismind = _registry({"Fermi": _record(["proposer"], independence_class="human")})
    result, conclair = await _mcp_write(
        settings, prismind, path="post", msg_type=msg_type, author="Fermi"
    )
    assert result["error_type"] == "EmbodimentRequiredError"
    conclair.post_message.assert_not_awaited()


# --- D(c): no stored attribute buys the exemption -------------------------
#
# The role is an ordinary one the record grants, so neither the human-role
# reservation nor the allowed_roles check answers first: the only reason a
# refusal can carry is the missing embodiment. Each case is paired with the
# same write *with* an embodiment, which must pass -- that is what shows the
# embodiment is the whole reason.

_FERMI_RECORDS = {
    "c-1 independence_class=human": _record(["proposer"], independence_class="human"),
    "c-2 allowed_roles has human": _record(
        ["proposer", "human"], independence_class="main-chain"
    ),
    "c-3 both, plus product_owner": _record(
        ["proposer", "human", "product_owner"], independence_class="human"
    ),
}


@pytest.mark.parametrize("record_case", list(_FERMI_RECORDS))
@pytest.mark.parametrize(
    ("path", "msg_type"),
    [("post", "handoff"), ("post", "ack"), ("post", "decide"), ("close", "decide")],
)
@pytest.mark.asyncio
async def test_a_stored_human_attribute_gives_fermi_no_exemption_over_mcp(
    settings, record_case, path, msg_type
) -> None:
    prismind = _registry({"Fermi": _FERMI_RECORDS[record_case]})

    refused, conclair = await _mcp_write(
        settings, prismind, path=path, msg_type=msg_type, author="Fermi", role="proposer"
    )
    assert refused["error_type"] == "EmbodimentRequiredError", refused
    conclair.post_message.assert_not_awaited()
    conclair.close_thread.assert_not_awaited()

    passed, conclair = await _mcp_write(
        settings, prismind, path=path, msg_type=msg_type, author="Fermi",
        role="proposer", embodiment="web_ai_chat",
    )
    assert "error_type" not in passed, passed
    written = conclair.close_thread if path == "close" else conclair.post_message
    written.assert_awaited_once()
    assert written.await_args.kwargs["role"] == "proposer"


@pytest.mark.parametrize("record_case", list(_FERMI_RECORDS))
@pytest.mark.parametrize("msg_type", STATE_TRANSITIONS)
@pytest.mark.asyncio
async def test_a_stored_human_attribute_gives_fermi_no_exemption_over_the_web(
    record_case, msg_type
) -> None:
    prismind = _registry({"Fermi": _FERMI_RECORDS[record_case]})
    form = {"type": msg_type, "author": "Fermi", "content": "c", "role": "proposer"}

    refused, conclair = await _web_post(prismind, form)
    assert "EmbodimentRequiredError" in refused.text
    conclair.post_message.assert_not_awaited()

    passed, conclair = await _web_post(prismind, {**form, "embodiment": "web_ai_chat"})
    assert "EmbodimentRequiredError" not in passed.text
    conclair.post_message.assert_awaited_once()
    assert conclair.post_message.await_args.kwargs["role"] == "proposer"


# --- D(d): only the identity ``human`` may newly claim role ``human`` ------


@pytest.mark.parametrize("path", ["post", "close"])
@pytest.mark.asyncio
async def test_fermi_claiming_role_human_is_refused_whatever_its_record(
    settings, path
) -> None:
    prismind = _registry(
        {"Fermi": _record(["proposer", "human"], independence_class="human")}
    )
    result, conclair = await _mcp_write(
        settings, prismind, path=path, msg_type="report", author="Fermi",
        role="human", embodiment="web_ai_chat",
    )
    assert result["error_type"] == "RoleNotAllowed"
    assert result["details"]["reason"] == "human_role_reserved"
    conclair.post_message.assert_not_awaited()
    conclair.close_thread.assert_not_awaited()
    # Refused before the record is read.
    prismind.get_identity.assert_not_awaited()


@pytest.mark.asyncio
async def test_fermi_claiming_role_human_is_refused_over_the_web() -> None:
    prismind = _registry({"Fermi": _record(["proposer", "human"])})
    response, conclair = await _web_post(
        prismind,
        {"type": "report", "author": "Fermi", "content": "c", "role": "human",
         "embodiment": "web_ai_chat"},
    )
    assert "RoleNotAllowed" in response.text
    conclair.post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_identity_human_may_claim_role_human(settings) -> None:
    prismind = _registry({"human": _record(["human"], independence_class="human")})
    result, conclair = await _mcp_write(
        settings, prismind, path="post", msg_type="report", author="human", role="human"
    )
    assert "error_type" not in result
    assert conclair.post_message.await_args.kwargs["role"] == "human"


# --- D(e): messages already stored with role ``human`` stay readable -------

_STORED = [
    {"msg_id": "msg-1", "author": "Fermi", "type": "decide", "role": "human", "content": "a"},
    {"msg_id": "msg-2", "author": "human", "type": "decide", "role": "human", "content": "b"},
]


@pytest.mark.asyncio
async def test_stored_role_human_messages_come_back_from_get_thread(settings) -> None:
    tools = _capture(chatroom_tools.register_tools, settings)
    conclair = _conclair()
    conclair.get_thread = AsyncMock(
        return_value={"thread": {"thread_id": THREAD}, "messages": list(_STORED), "mode": "full"}
    )
    with patch.object(chatroom_tools, "_adapter", return_value=conclair):
        view = await tools["chatroom_get_thread"](project=PROJECT, thread_id=THREAD)
    assert "error_type" not in view
    assert [(m["author"], m["role"]) for m in view["messages"]] == [
        ("Fermi", "human"), ("human", "human"),
    ]


@pytest.mark.asyncio
async def test_stored_role_human_messages_come_back_from_list_events(settings) -> None:
    tools = _capture(chatroom_tools.register_tools, settings)
    events = [
        {"id": i, "action": "post_message", "payload": msg} for i, msg in enumerate(_STORED)
    ]
    conclair = _conclair()
    conclair.list_events = AsyncMock(
        return_value={"items": events, "total": 2, "limit": 100, "offset": 0}
    )
    with patch.object(chatroom_tools, "_adapter", return_value=conclair):
        listing = await tools["chatroom_list_events"](project=PROJECT, thread_id=THREAD)
    assert listing["total"] == 2
    assert [e["payload"]["role"] for e in listing["items"]] == ["human", "human"]
    assert [e["payload"]["author"] for e in listing["items"]] == ["Fermi", "human"]


@pytest.mark.asyncio
async def test_stored_role_human_messages_come_back_from_the_web_thread_view() -> None:
    page = (
        '<div data-msg="msg-1" data-author="Fermi" data-role="human">a</div>'
        '<div data-msg="msg-2" data-author="human" data-role="human">b</div>'
    )
    chatroom_proxy._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, text=page)),
        base_url="http://localhost:8115",
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()),
            base_url="http://testserver",
        ) as client:
            response = await client.get(f"/ui/projects/{PROJECT}/threads/{THREAD}")
    finally:
        chatroom_proxy._client = None
    assert response.status_code == 200
    assert response.text == page


# --- D(f): upsert_identity never grants role ``human`` elsewhere ----------


@pytest.mark.parametrize(
    ("identity_name", "kwargs", "granted"),
    [
        ("Fermi", {"allowed_roles": ["human"]}, False),
        ("Fermi", {"allowed_roles": ["proposer", "human"]}, False),
        ("Fermi", {"keep_allowed_roles": True, "allowed_roles": ["human"]}, False),
        ("Fermi", {"keep_allowed_roles": True}, True),
        ("Fermi", {"allowed_roles": ["proposer", "product_owner"]}, True),
        ("human", {"allowed_roles": ["human"]}, True),
        ("Human", {"allowed_roles": ["human"]}, True),
    ],
)
@pytest.mark.asyncio
async def test_upsert_identity_grants_role_human_only_to_the_identity_human(
    identity_name, kwargs, granted
) -> None:
    tools = _capture(session_tools.register_tools, Settings(prismind_url="http://x"))
    with patch.object(session_tools, "PrismindAdapter") as adapter_cls:
        upstream = adapter_cls.return_value
        upstream.upsert_identity = AsyncMock(
            return_value={"success": True, "identity": {}, "created": False, "message": "ok"}
        )
        result = await tools["upsert_identity"](
            identity_name=identity_name, independence_class="human", **kwargs
        )
    assert result["success"] is granted, result
    if granted:
        upstream.upsert_identity.assert_awaited_once()
    else:
        assert "human" in result["message"]
        upstream.upsert_identity.assert_not_awaited()


# --- D(g): closer x owner on the settled-close carve-out -------------------

_CLOSERS = {
    "Heisenberg": _record(["implementer"]),
    "PO": _record(["product_owner"]),
    "Einstein": _record(["naysayer"], independence_class="independent"),
}

_OWNERS = {
    "Bohr": _record(["proposer"]),
    "FermiWithHuman": _record(["proposer", "human"], independence_class="human"),
    "FermiWithoutHuman": _record(["proposer", "reviewer"], independence_class="human"),
    "Owner": _record(["product_owner"]),
}


async def _settled(closer: str, owner: str) -> tuple[dict[str, Any], MagicMock]:
    adapter = _settled_adapter(thread=_settled_thread(owner=owner))
    result, adapter, _ = await _settled_close(
        settings=_settled_settings(closers=list(_CLOSERS)),
        adapter=adapter,
        prismind=_registry({**_CLOSERS, **_OWNERS}),
        author=closer,
    )
    return result, adapter


@pytest.mark.parametrize("closer", ["Heisenberg", "PO"])
@pytest.mark.asyncio
async def test_a_closing_role_closes_an_ordinary_owners_settled_thread(closer) -> None:
    result, adapter = await _settled(closer, "Bohr")
    assert "error_type" not in result, result
    adapter.close_thread.assert_awaited_once()
    assert adapter.close_thread.await_args.kwargs["author"] == closer


@pytest.mark.parametrize(
    ("owner", "keys"),
    [
        ("FermiWithHuman", ["independence_class", "allowed_roles:human"]),
        ("FermiWithoutHuman", ["independence_class"]),
        ("Owner", ["allowed_roles:product_owner"]),
        ("human", ["identity_name"]),
    ],
)
@pytest.mark.parametrize("closer", ["Heisenberg", "PO"])
@pytest.mark.asyncio
async def test_no_closer_closes_a_protected_owners_thread(closer, owner, keys) -> None:
    """Before and after ``human`` leaves Fermi's record (FermiWithHuman vs
    FermiWithoutHuman) the thread stays protected."""
    result, adapter = await _settled(closer, owner)
    assert _settled_reason(result) == sd.REASON_OWNER_HUMAN
    assert result["details"]["protection_keys"] == keys
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_naysayer_is_refused_as_a_closer() -> None:
    result, adapter = await _settled("Einstein", "Bohr")
    assert result["error_type"] == "RoleNotAllowedToClose"
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_record_without_independence_class_is_judged_on_its_roles() -> None:
    """Einstein msg-1800: None does not match, the other key still decides."""
    adapter = _settled_adapter(thread=_settled_thread(owner="Bare"))
    registry = {**_CLOSERS, "Bare": _record(["proposer"], independence_class=None)}
    result, adapter, _ = await _settled_close(
        settings=_settled_settings(closers=list(_CLOSERS)),
        adapter=adapter, prismind=_registry(registry), author="PO",
    )
    assert "error_type" not in result, result
    adapter.close_thread.assert_awaited_once()


@pytest.mark.parametrize(("author", "allowed"), [("PO", True), ("Einstein", False)])
@pytest.mark.asyncio
async def test_the_second_stage_admits_product_owner_and_not_the_naysayer(
    author, allowed
) -> None:
    with patch.object(chatroom_tools, "_prismind_adapter", return_value=_registry(_CLOSERS)):
        decision = await chatroom_tools._check_close_permitted(author=author, role="")
    if allowed:
        assert decision.error is None
    else:
        assert decision.error["error_type"] == "RoleNotAllowedToClose"


# --- D(h) ----------------------------------------------------------------


def test_closeable_roles_has_product_owner_and_never_human() -> None:
    assert "product_owner" in chatroom_tools.CLOSEABLE_ROLES
    assert "human" not in chatroom_tools.CLOSEABLE_ROLES
