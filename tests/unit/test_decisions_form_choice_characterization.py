"""Characterization tests (I-18) — pin the wire bytes of the msg-121 tap.

**Order matters** (msg-146 §4 / I-18): pin the existing behavior with
tests *before* rewriting the form. Writing tests after the change fixes
the new behavior as "correct" — the whole point of a characterization
suite is to catch the accidental drift the rewrite creates. spec/slices
S5-decision-page §8 rested on 1 human tap; every byte we lock here is
guaranteed by that tap and cannot be re-measured cheaply.

Scope of what this file locks (Bohr msg-146 §4 pick list):

1. **Blank body is rejected** — a bare ``content=`` (missing) POST to the
   shared handler stops at FastAPI (422) before running any of our
   opt-in code. This is the "空欄送信の欠陥" fix from msg-121.
2. **Freeform-only wire bytes** — the exact composed body the msg-102
   regression test locks (sentinel + empty ``_freeform`` + a
   ``next_participant`` → ``NEXT: Bohr`` only) is what Takahito's tap
   confirmed live. Duplicated here so a rename of the msg-102 test
   cannot silently drop the pin. (This particular assertion overlaps
   with ``test_msg102_regression_empty_freeform_with_sentinel_fires_opt_in``
   in ``test_decisions_form.py`` on purpose — two independent handles.)
3. **G-1 bit-identical POST** — a POST without ``_decision_form`` reaches
   the adapter with the raw ``content`` value untouched and does *not*
   redirect. This overlaps with ``test_g1_existing_post_without_
   decision_form_reaches_conclair_unchanged`` for the same reason (I-18
   requires the pins to be locally-obvious in the file that guards the
   rewrite).

If any of the three fails after the D-35 / D-36 change, the rewrite has
broken a shipped guarantee (spec §3.2 G-1 or msg-121's implementer-level
"空欄送信は拒否される"), and the fix is to restore that guarantee — not
to update the test.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.mcp.tools import chatroom as chatroom_tools
from magickit.main import create_app


PROJECT = "spirrow-magickit"
THREAD = "T-decide"


@pytest.fixture(autouse=True)
def _configured():
    chatroom_tools.configure(Settings())
    yield
    chatroom_tools._settings = None


def _passing_gate(role: str | None = None):
    return AsyncMock(return_value=chatroom_tools._RoleDecision(error=None, role=role))


async def _post(path: str, data: dict[str, str]) -> httpx.Response:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        return await client.post(path, data=data)


# --- Pin 1: blank body is rejected (msg-121 fix) -----------------------


@pytest.mark.asyncio
async def test_i18_blank_content_field_rejected_before_handler_runs():
    """spec §3.1a empirical / msg-121 fix: an entirely missing ``content``
    field lands as FastAPI's ``{"type":"missing","input":null}`` 422.

    The rewrite (D-35 radio + single submit) MUST preserve this: the
    handler shape is a shared endpoint whose ``content`` param has no
    default, and if the rewrite loosens ``content`` to ``Optional``, the
    existing MCP-less browser proxy write shape breaks in ways only live
    Conclair would catch. Keep this assertion tight (422, not 400).
    """
    r = await _post(
        f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
        {
            "type": "decide",
            "author": "human",
            # No ``content`` field at all.
        },
    )
    assert r.status_code == 422
    body = r.json()
    # The specific field name — a rename of the handler param is a
    # breaking contract change and this pin catches it.
    details = body.get("detail", [])
    assert any(
        d.get("loc") == ["body", "content"] and d.get("type") == "missing"
        for d in details
    ), body


# --- Pin 2: msg-121 freeform-only wire bytes (Takahito's tap) ----------


@pytest.mark.asyncio
async def test_i18_msg121_freeform_only_wire_bytes_stable():
    """The exact composed body that msg-121's live tap produced.

    Same assertion as the msg-102 regression test, kept independently
    here so the file that guards the rewrite has the pin locally. If the
    D-35 rewrite changes the sentinel value or its normalisation, both
    this test and the msg-102 regression will fail — and both files will
    surface in the diff, which is the point.
    """
    adapter = AsyncMock()
    adapter.post_message = AsyncMock(
        return_value={"msg": {"msg_id": "m-i18-freeform", "type": "decide"}}
    )
    adapter.close = AsyncMock()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(chatroom_tools, "_check_next_participant", AsyncMock(return_value=None)),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        r = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {
                "type": "decide",
                "author": "human",
                "_decision_form": "1",
                "content": "(自由記述のみ)",
                "_freeform": "",
                "next_participant": "Bohr",
            },
        )

    assert r.status_code == 303  # opt-in fired
    kwargs = adapter.post_message.call_args.kwargs
    assert kwargs["content"] == "NEXT: Bohr"


# --- Pin 3: G-1 bit-identical POST (Einstein §2 / spec §3.2) -----------


@pytest.mark.asyncio
async def test_i18_g1_existing_post_without_decision_form_stays_legacy():
    """spec §3.2 / msg-103: absence of ``_decision_form`` = legacy path.

    The rewrite MUST NOT make the opt-in fire on legacy POSTs. Even if
    ``content`` happens to equal the sentinel by accident, the legacy
    path must pass it through unchanged. Locking this here so the D-31
    exhaustive-fallback work does not accidentally special-case sentinel
    detection outside the opt-in branch.
    """
    adapter = AsyncMock()
    adapter.post_message = AsyncMock(
        return_value={"msg": {"msg_id": "m-i18-g1", "type": "question"}}
    )
    adapter.close = AsyncMock()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(chatroom_tools, "_check_next_participant", AsyncMock(return_value=None)),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        r = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {
                "type": "question",
                "author": "human",
                # No _decision_form; content happens to look like sentinel.
                "content": "(自由記述のみ)",
                "_freeform": "should not be spliced",
            },
        )

    assert r.status_code == 200  # legacy flash, not 303
    kwargs = adapter.post_message.call_args.kwargs
    # Bit-identical: sentinel-shaped string reaches Conclair verbatim
    # because we did NOT enter the opt-in branch.
    assert kwargs["content"] == "(自由記述のみ)"
