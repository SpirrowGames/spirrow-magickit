"""Tests for the mojibake warning on chatroom writes.

The detector exists because Starlette's urlencoded parser decodes the raw
body as latin-1 before percent-decoding (correct per the spec, which
assumes a percent-encoded ASCII body) -- so a client that puts raw UTF-8
bytes in the body has them stored as the latin-1 characters they mapped
to. Observed in `scratch-ui-write-probe` on 2026-08-03: three messages,
the only corrupted rows in the archive.

Half of these tests are about what must *not* be flagged. A warning that
fires on ordinary French is a warning people learn to skip, and this one
has to survive being ignored for months and still be believed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.main import create_app
from magickit.mcp.tools import chatroom as chatroom_tools
from magickit.web import chatroom_writes
from magickit.web.mojibake import first_mojibake, recover_mojibake

# The exact bytes that reach a handler when `curl -d "content=経由の書き込み"`
# posts raw UTF-8 into an urlencoded body.
MANGLED = "経由の書き込み".encode("utf-8").decode("latin-1")


@pytest.fixture(autouse=True)
def _configured():
    chatroom_tools.configure(Settings())
    yield
    chatroom_tools._settings = None


# --- the detector ---------------------------------------------------------


def test_the_real_corruption_is_recovered():
    assert recover_mojibake(MANGLED) == "経由の書き込み"


def test_the_archives_actual_rows_are_recovered():
    """The three messages found in scratch-ui-write-probe, verbatim."""
    rows = {
        "magickit çµ\x8cç\x94±ã\x81®æ\x9b¸ã\x81\x8dè¾¼ã\x81¿çµ\x8cè·¯ã\x81®ç\x96\x8eé\x80\x9aç¢ºèª\x8d": (
            "magickit 経由の書き込み経路の疎通確認"
        ),
        "human ã\x81\x8bã\x82\x89ã\x81®æ\x8a\x95ç¨¿ã\x81\x8cé\x80\x9aã\x82\x8bã\x81\x8b": (
            "human からの投稿が通るか"
        ),
    }
    for stored, expected in rows.items():
        assert recover_mojibake(stored) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain ascii only",
        "経由の書き込み",  # correct Japanese -- not latin-1 encodable
        "done ✅ 🎉",
        "Café au lait: café, crème brûlée",
        "Übergrößenträger, Straße, Weiß",
        "¿Dónde está el niño? ¡Añádelo!",
        "Blåbærsyltetøj på smørrebrød",
        "Conceição, informação, José",
        "£100 ± 5%, ©2026, ½ × ¾, µs",
        'printf("%s\\n", buf); // café',
    ],
)
def test_correct_text_is_never_flagged(text):
    """European text survives because "é" is followed by an ASCII letter,
    which is not a valid UTF-8 continuation byte."""
    assert recover_mojibake(text) is None


def test_a_recovery_producing_control_bytes_is_not_reported():
    """The round trip succeeding is not the same as it meaning something."""
    assert recover_mojibake("Â\x81") is None


def test_first_mojibake_names_the_field():
    assert first_mojibake({"title": "ok", "content": MANGLED}) == (
        "content",
        "経由の書き込み",
    )


def test_first_mojibake_returns_none_when_everything_is_clean():
    assert first_mojibake({"title": "ok", "content": "経由"}) is None


# --- the warning on the write path ----------------------------------------


def _adapter_ok(**result):
    adapter = AsyncMock()
    adapter.post_message.return_value = result or {"msg": {"msg_id": "msg-9"}}
    adapter.open_thread.return_value = {"thread": {}, "msg": {"msg_id": "msg-1"}}
    return adapter


async def _post(path: str, data: dict, adapter) -> httpx.Response:
    app = create_app()
    with patch.object(chatroom_writes, "_adapter", return_value=adapter):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            return await client.post(path, data=data)


@pytest.mark.asyncio
async def test_a_mangled_body_still_gets_written():
    """Rejecting would make it impossible to post an example of mojibake,
    which this team does when discussing encoding incidents."""
    adapter = _adapter_ok()

    response = await _post(
        "/ui/projects/p/threads/T/messages",
        {"type": "report", "author": "human", "content": MANGLED},
        adapter,
    )

    adapter.post_message.assert_awaited_once()
    assert adapter.post_message.await_args.kwargs["content"] == MANGLED
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_the_warning_carries_the_recovered_text():
    """A warning that only says "something is wrong" cannot be acted on --
    and it cannot be acted on later either, since the log is append-only."""
    body = (
        await _post(
            "/ui/projects/p/threads/T/messages",
            {"type": "report", "author": "human", "content": MANGLED},
            _adapter_ok(),
        )
    ).text

    assert "投稿は成功しました" in body
    assert "経由の書き込み" in body
    assert "--data-urlencode" in body


@pytest.mark.asyncio
async def test_the_warning_does_not_use_the_auto_dismissing_class():
    """conclair.js removes `.alert-success` after six seconds. A warning
    that disappears on its own is not a warning."""
    body = (
        await _post(
            "/ui/projects/p/threads/T/messages",
            {"type": "report", "author": "human", "content": MANGLED},
            _adapter_ok(),
        )
    ).text

    assert '<div class="alert alert-error"><strong>投稿は成功しました' in body


@pytest.mark.asyncio
async def test_a_clean_post_gets_no_warning():
    body = (
        await _post(
            "/ui/projects/p/threads/T/messages",
            {"type": "report", "author": "human", "content": "経由の書き込み"},
            _adapter_ok(),
        )
    ).text

    assert "alert-success" in body
    assert "文字化け" not in body


@pytest.mark.asyncio
async def test_open_thread_checks_the_title_too():
    body = (
        await _post(
            "/ui/projects/p/threads",
            {
                "thread_id": "T-x",
                "title": MANGLED,
                "owner": "human",
                "propose_content": "clean",
            },
            _adapter_ok(),
        )
    ).text

    assert "文字化け" in body
    assert "<code>title</code>" in body


@pytest.mark.asyncio
async def test_the_recovered_text_is_escaped():
    """It is attacker-influenced text going into an HTML fragment."""
    payload = "<script>alert(1)</script>経由".encode("utf-8").decode("latin-1")

    body = (
        await _post(
            "/ui/projects/p/threads/T/messages",
            {"type": "report", "author": "human", "content": payload},
            _adapter_ok(),
        )
    ).text

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body
