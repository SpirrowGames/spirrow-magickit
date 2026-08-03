"""Unit tests for the dashboard's chatroom panel.

The panel's job is to say which projects need a human, so what matters is
the ranking and the "needs attention" column -- not the raw totals. It
also has to degrade quietly: Conclair being down must not take the whole
dashboard with it.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.main import create_app
from magickit.mcp.tools import chatroom as chatroom_tools


@pytest.fixture(autouse=True)
def _configured():
    chatroom_tools.configure(Settings())
    yield
    chatroom_tools._settings = None


async def _get_panel() -> httpx.Response:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.get("/dashboard/chatroom")


def _summary(**kw):
    entry = {
        "project": "p",
        "thread_count": 1,
        "threads_by_status": {"active": 1},
        "gated_thread_count": 0,
        "message_count": 1,
        "last_activity_at": None,
    }
    entry.update(kw)
    return entry


def _adapter_returning(payload):
    adapter = AsyncMock()
    adapter.list_project_summaries.return_value = payload
    return adapter


@pytest.mark.asyncio
async def test_projects_with_more_open_work_rank_first():
    payload = {
        "items": [
            _summary(project="quiet", threads_by_status={"resolved": 40}),
            _summary(project="busy", threads_by_status={"active": 7}),
        ],
        "total": 2,
    }

    with patch.object(
        chatroom_tools, "_adapter", return_value=_adapter_returning(payload)
    ):
        body = (await _get_panel()).text

    assert body.index("busy") < body.index("quiet")


@pytest.mark.asyncio
async def test_open_count_excludes_resolved():
    payload = {
        "items": [
            _summary(
                project="p",
                thread_count=10,
                threads_by_status={"active": 2, "awaiting_reply": 1, "resolved": 7},
            )
        ],
        "total": 1,
    }

    with patch.object(
        chatroom_tools, "_adapter", return_value=_adapter_returning(payload)
    ):
        body = (await _get_panel()).text

    # 3 open (2 active + 1 awaiting), 10 total -- both present, not conflated.
    assert '<td data-label="open">3</td>' in body
    assert '<td data-label="threads">10</td>' in body


@pytest.mark.asyncio
async def test_gated_and_awaiting_are_badged():
    payload = {
        "items": [
            _summary(
                project="p",
                threads_by_status={"active": 3, "awaiting_reply": 2},
                gated_thread_count=1,
            )
        ],
        "total": 1,
    }

    with patch.object(
        chatroom_tools, "_adapter", return_value=_adapter_returning(payload)
    ):
        body = (await _get_panel()).text

    assert "2 awaiting reply" in body
    assert "1 gated" in body


@pytest.mark.asyncio
async def test_project_links_into_the_chatroom_ui():
    payload = {"items": [_summary(project="spirrow-mindwire")], "total": 1}

    with patch.object(
        chatroom_tools, "_adapter", return_value=_adapter_returning(payload)
    ):
        body = (await _get_panel()).text

    assert '/ui/projects/spirrow-mindwire/threads' in body


@pytest.mark.asyncio
async def test_project_name_is_escaped():
    payload = {"items": [_summary(project='<script>x</script>')], "total": 1}

    with patch.object(
        chatroom_tools, "_adapter", return_value=_adapter_returning(payload)
    ):
        body = (await _get_panel()).text

    assert "<script>x</script>" not in body
    assert "&lt;script&gt;" in body


@pytest.mark.asyncio
async def test_row_cap_reports_what_it_hid():
    """A truncated list must say so rather than read as the whole picture."""
    payload = {
        "items": [_summary(project=f"p{i}") for i in range(12)],
        "total": 12,
    }

    with patch.object(
        chatroom_tools, "_adapter", return_value=_adapter_returning(payload)
    ):
        body = (await _get_panel()).text

    assert "+4 more" in body


@pytest.mark.asyncio
async def test_conclair_outage_degrades_to_a_notice():
    adapter = AsyncMock()
    adapter.list_project_summaries.side_effect = httpx.ConnectError("down")

    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        response = await _get_panel()

    assert response.status_code == 200
    assert "unavailable" in response.text


@pytest.mark.asyncio
async def test_conclair_error_envelope_degrades_to_a_notice():
    payload = {"error_type": "ChatroomDBError", "error": "pool exhausted"}

    with patch.object(
        chatroom_tools, "_adapter", return_value=_adapter_returning(payload)
    ):
        response = await _get_panel()

    assert response.status_code == 200
    assert "unavailable" in response.text
    assert "pool exhausted" in response.text


@pytest.mark.asyncio
async def test_missing_endpoint_is_not_reported_as_no_activity():
    """A stale Conclair 404s; that is a deploy fact, not a data fact."""
    with patch.object(
        chatroom_tools,
        "_adapter",
        return_value=_adapter_returning({"detail": "Not Found"}),
    ):
        body = (await _get_panel()).text

    assert "no chatroom activity yet" not in body
    assert "unavailable" in body


@pytest.mark.asyncio
async def test_no_projects_yet():
    with patch.object(
        chatroom_tools, "_adapter", return_value=_adapter_returning({"items": [], "total": 0})
    ):
        body = (await _get_panel()).text

    assert "no chatroom activity yet" in body


# ---- phone layout ---------------------------------------------------------
#
# The stacked (phone) table renders each cell's label from its `data-label`,
# so a column's name lives twice: in the `<th>` and in the cell. Drift is
# invisible in a browser -- the desktop table stays correct while the phone
# view starts labelling values wrongly.

_TH_RE = re.compile(r"<th(?:\s[^>]*)?>(.*?)</th>", re.DOTALL)
_LABEL_RE = re.compile(r'<td[^>]*\bdata-label="([^"]*)"')


@pytest.mark.asyncio
async def test_panel_labels_match_its_headers():
    payload = {"items": [_summary()], "total": 1}

    with patch.object(
        chatroom_tools, "_adapter", return_value=_adapter_returning(payload)
    ):
        body = (await _get_panel()).text

    headers = [re.sub(r"\s+", " ", h).strip() for h in _TH_RE.findall(body)]
    assert _LABEL_RE.findall(body) == headers


@pytest.mark.asyncio
async def test_panel_opts_into_stacking():
    """Without the class the labels render but nothing reads them."""
    payload = {"items": [_summary()], "total": 1}

    with patch.object(
        chatroom_tools, "_adapter", return_value=_adapter_returning(payload)
    ):
        body = (await _get_panel()).text

    assert 'class="table table-stack"' in body
