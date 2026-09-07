"""Close-detection SSOT (T-close-detection-truthiness-seam).

Spec: T-close-detection-truthiness-seam msg-243 (Bohr, carve out ①) and
msg-244 (Einstein's advisory: eliminate the truthiness ambiguity at
Magickit's *own* ingress; do not paper over the seam by mimicking
Conclair's edge-case string parsing).

Two things this file pins:

1. ``_normalize_closes_thread`` + ``_is_close_post`` are the single-source-
   of-truth close-detection helpers on the Magickit side. Both ingress
   sites (the MCP ``chatroom_post_message`` tool and the browser POST
   handler in ``web/chatroom_writes.py``) route their "is this a close?"
   decision through ``_is_close_post`` — replacing the stray
   ``bool(closes_thread)`` copies that msg-243 identified as dual
   management (Principle 2).

2. The Conclair-side baseline that msg-243 Step 1 asked for is
   *documented* here (not mimicked). Conclair's close detection lives at
   ``spirrow_conclair.services.integrity.assert_closes_thread_rule``
   (``closes_thread is None`` short-circuits as "not a close"; anything
   else — including ``""`` — is a close attempt checked against
   ``thread_id``) and ``spirrow_conclair.services.status_transition.
   compute_transition`` (``type == "decide" and closes_thread ==
   thread.thread_id`` — an exact ``thread_id`` match is what actually
   resolves the thread). Magickit does NOT re-encode those rules — it
   normalizes at ingress so Conclair only ever receives ``None`` or a
   non-empty string, and the empty-string edge case Conclair tolerates
   cannot arise on this wire in the first place. That is why the SSOT
   helpers are as small as they are.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.main import create_app
from magickit.mcp.tools import chatroom as chatroom_tools


PROJECT = "spirrow-magickit"
THREAD = "T-1"


# --- pure helpers ------------------------------------------------------


class TestNormalizeClosesThread:
    """``_normalize_closes_thread`` collapses ingress ``str | None`` to
    the canonical ``str | None`` (``None`` = "not a close")."""

    def test_none_is_none(self) -> None:
        assert chatroom_tools._normalize_closes_thread(None) is None

    def test_empty_string_is_none(self) -> None:
        # Both the MCP tool signature and the FastAPI form spell the
        # default as ``str = ""``, so ``""`` is the "field omitted" case
        # in practice and must collapse to None.
        assert chatroom_tools._normalize_closes_thread("") is None

    def test_thread_id_passes_through(self) -> None:
        assert chatroom_tools._normalize_closes_thread("T-1") == "T-1"

    def test_non_matching_string_passes_through(self) -> None:
        # Deliberate: this helper is Magickit's canonicalization, not
        # Conclair's semantic check. ``"T-other"`` reaches Conclair
        # verbatim, which will reject it with ChatroomIntegrityError.
        # The point is that Magickit doesn't try to know that here.
        assert chatroom_tools._normalize_closes_thread("T-other") == "T-other"

    def test_whitespace_only_string_passes_through(self) -> None:
        # Preserved as-is on purpose: bool("   ") was True in the old
        # code path, so Conclair already had to cope with this value; we
        # do not silently change its meaning on the wire in this fix.
        # (Conclair strips whitespace at its own schema layer.)
        assert chatroom_tools._normalize_closes_thread("   ") == "   "


class TestIsClosePost:
    """``_is_close_post`` is the single "is this a close?" predicate."""

    @pytest.mark.parametrize(
        "msg_type",
        ["propose", "question", "answer", "report", "handoff", "ack"],
    )
    def test_non_decide_is_never_close(self, msg_type: str) -> None:
        # Only ``decide`` closes threads in Conclair's status_transition;
        # this predicate must agree on that side of the fence too.
        assert chatroom_tools._is_close_post(msg_type, "T-1") is False

    def test_decide_with_none_is_not_close(self) -> None:
        assert chatroom_tools._is_close_post("decide", None) is False

    def test_decide_with_normalized_string_is_close(self) -> None:
        assert chatroom_tools._is_close_post("decide", "T-1") is True

    def test_expects_pre_normalized_input(self) -> None:
        # Documents the contract: callers MUST normalize first. Because
        # normalization collapses ``""`` to ``None``, this helper never
        # sees ``""`` in the live paths; but if it did, ``is not None``
        # would misclassify it. The two ingress sites are covered by the
        # SSOT test below, which is how we guarantee the contract holds.
        assert chatroom_tools._is_close_post("decide", "") is True


# --- SSOT: both ingress sites route through _is_close_post -----------


def _identity_response(
    allowed_roles: list[str], *, name: str = "Bohr"
) -> dict[str, Any]:
    return {
        "success": True,
        "found": True,
        "identity": {
            "identity_name": name,
            "user": "sgadmin",
            "allowed_roles": allowed_roles,
            "independence_class": "independent",
            "persona_description": "",
        },
        "message": "ok",
    }


@pytest.fixture(autouse=True)
def _configured():
    """Gates read module-level settings; the app normally binds them at startup."""
    chatroom_tools.configure(Settings())
    yield
    chatroom_tools._settings = None


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


def _chat_adapter() -> MagicMock:
    a = MagicMock()
    a.open_thread = AsyncMock(return_value={"thread": {}, "msg": {}})
    a.post_message = AsyncMock(
        return_value={"msg": {"msg_id": "m", "type": "report"}, "thread_status_changed_to": None}
    )
    a.close_thread = AsyncMock(return_value={"thread": {}, "decide_msg": {}})
    a.get_thread = AsyncMock(
        return_value={"thread": {"tags": []}, "messages": [], "mode": "full"}
    )
    a.close = AsyncMock()
    return a


def _prismind_adapter(allowed_roles: list[str], name: str = "Bohr") -> MagicMock:
    a = MagicMock()
    a.get_identity = AsyncMock(return_value=_identity_response(allowed_roles, name=name))
    return a


def _call_real(msg_type: str, closes_thread: str | None) -> bool:
    """Standalone re-implementation of the predicate used only by the
    spy; the spy patches the module attribute so it cannot call the
    real one without recursion. If a future edit changes the predicate,
    this stays in lockstep because the tests that depend on it also
    cover the pure helper directly (``TestIsClosePost``)."""
    return msg_type == "decide" and closes_thread is not None


@pytest.mark.asyncio
async def test_mcp_post_message_routes_close_decision_through_ssot_helper() -> None:
    """The MCP ``chatroom_post_message`` tool must ask ``_is_close_post``
    (not its own inline ``bool(closes_thread)``) whether the post closes."""
    settings = Settings(
        conclair_url="http://localhost:8115",
        conclair_timeout=5.0,
        prismind_url="http://localhost:8002",
        prismind_timeout=5.0,
    )
    tools = _capture_tools(settings)
    chat = _chat_adapter()
    prismind = _prismind_adapter(["proposer"])

    calls: list[tuple[str, str | None]] = []

    def spy(msg_type: str, closes_thread: str | None) -> bool:
        calls.append((msg_type, closes_thread))
        return _call_real(msg_type, closes_thread)

    with (
        patch.object(chatroom_tools, "_adapter", return_value=chat),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
        patch.object(chatroom_tools, "_is_close_post", side_effect=spy) as mock_pred,
    ):
        await tools["chatroom_post_message"](
            project=PROJECT, thread_id=THREAD, msg_type="report",
            author="Bohr", content="c", role="proposer",
        )

    assert mock_pred.called, (
        "MCP tool must call the SSOT predicate; a stray bool(closes_thread) "
        "copy would leave the spy uncalled."
    )
    # The MCP tool must have normalized the ingress string before asking:
    # ``closes_thread=""`` (the default) becomes ``None`` in the call.
    assert calls[0] == ("report", None)


@pytest.mark.asyncio
async def test_browser_post_routes_close_decision_through_ssot_helper() -> None:
    """The browser POST handler must ask ``_is_close_post`` too. This is
    the site msg-243 flagged as the duplicate — pinning it here is the
    "the two never drift" invariant."""
    chat = _chat_adapter()
    prismind = _prismind_adapter(["proposer"])

    calls: list[tuple[str, str | None]] = []

    def spy(msg_type: str, closes_thread: str | None) -> bool:
        calls.append((msg_type, closes_thread))
        return _call_real(msg_type, closes_thread)

    app = create_app()
    with (
        patch.object(chatroom_tools, "_adapter", return_value=chat),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
        patch.object(chatroom_tools, "_is_close_post", side_effect=spy) as mock_pred,
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            await client.post(
                f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
                data={
                    "type": "report", "author": "Bohr",
                    "content": "c", "role": "proposer",
                },
            )

    assert mock_pred.called, (
        "Browser POST must call the SSOT predicate; a stray bool(closes_thread) "
        "copy would leave the spy uncalled."
    )
    assert calls[0] == ("report", None)


@pytest.mark.asyncio
async def test_mcp_tool_forwards_normalized_closes_thread_to_adapter() -> None:
    """Ingress normalization must reach the adapter — the same normalized
    value the SSOT predicate saw goes on the wire to Conclair. This is
    what removes the two-locations-doing-`bool(...)` seam: there is a
    single ``str | None`` value flowing from ingress to Conclair."""
    settings = Settings(
        conclair_url="http://localhost:8115",
        conclair_timeout=5.0,
        prismind_url="http://localhost:8002",
        prismind_timeout=5.0,
    )
    tools = _capture_tools(settings)
    chat = _chat_adapter()
    prismind = _prismind_adapter(["proposer"])

    with (
        patch.object(chatroom_tools, "_adapter", return_value=chat),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
    ):
        # Empty ingress → None on the wire.
        await tools["chatroom_post_message"](
            project=PROJECT, thread_id=THREAD, msg_type="report",
            author="Bohr", content="c", closes_thread="",
            role="proposer",
        )

    assert chat.post_message.call_args.kwargs["closes_thread"] is None


@pytest.mark.asyncio
async def test_browser_post_forwards_normalized_closes_thread_to_adapter() -> None:
    """Twin of the previous test on the browser POST path."""
    chat = _chat_adapter()
    prismind = _prismind_adapter(["proposer"])

    app = create_app()
    with (
        patch.object(chatroom_tools, "_adapter", return_value=chat),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            await client.post(
                f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
                data={
                    "type": "report", "author": "Bohr", "content": "c",
                    "closes_thread": "", "role": "proposer",
                },
            )

    assert chat.post_message.call_args.kwargs["closes_thread"] is None


# --- no stray bool(closes_thread) copies -------------------------------


def test_no_bool_closes_thread_copies_in_ingress_sites() -> None:
    """Belt-and-braces text pin: neither ingress site contains a stray
    ``bool(closes_thread)``. If a future edit reintroduces one, this test
    fails and the reviewer sees that dual management is back."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "magickit"
    for rel in ("mcp/tools/chatroom.py", "web/chatroom_writes.py"):
        text = (root / rel).read_text(encoding="utf-8")
        # Strip out the running commentary that intentionally references
        # the historical pattern — comments alone must not fail the pin,
        # but a real call would.
        code_lines = [
            line for line in text.splitlines()
            if not line.lstrip().startswith("#")
        ]
        code = "\n".join(code_lines)
        assert "bool(closes_thread)" not in code, (
            f"{rel} contains a stray bool(closes_thread) — that's the very "
            "dual-management seam T-close-detection-truthiness-seam removed. "
            "Call _is_close_post on a normalized value instead."
        )
