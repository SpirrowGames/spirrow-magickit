"""start_task's attachment refresh only touches this task's attachments.

The lookup is a semantic search over category="task_attachment", so it
returns whatever reads as similar -- including entries recorded for other
tasks. The UTID stored inside each entry is what decides membership.

Letting a sibling through is not merely a display problem: a changed file
is re-recorded with tags and source rebuilt from the *caller's* UTID
while the entry's own content still names the other task, so the stored
knowledge ends up contradicting its own tags.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.mcp.tools import task

MINE = "folder-uid-1:phase1:T01"
THEIRS = "folder-uid-1:phase1:T99"


def _attachment(utid: str, path: Path, file_hash: str) -> dict[str, Any]:
    return {
        "id": f"k-{utid}-{path.name}",
        "content": json.dumps({
            "file_path": str(path),
            "file_hash": file_hash,
            "doc_id": f"doc-{path.name}",
            "utid": utid,
        }),
        "category": "task_attachment",
    }


@pytest.fixture
def settings() -> MagicMock:
    s = MagicMock()
    s.prismind_url = "http://localhost:8112"
    s.prismind_timeout = 30.0
    s.cognilens_url = "http://localhost:8111"
    s.cognilens_timeout = 30.0
    return s


@pytest.mark.asyncio
async def test_sibling_task_attachment_is_ignored(settings, tmp_path):
    """An entry carrying another task's UTID is not reported as ours."""
    mine = tmp_path / "mine.py"
    mine.write_text("mine")
    theirs = tmp_path / "theirs.py"
    theirs.write_text("theirs")

    prismind = AsyncMock()
    prismind.search_knowledge = AsyncMock(return_value=[
        _attachment(MINE, mine, task._calculate_file_hash(mine.read_bytes())),
        _attachment(THEIRS, theirs, task._calculate_file_hash(theirs.read_bytes())),
    ])

    status = await task._refresh_task_attachments(
        settings=settings, prismind=prismind, utid=MINE, project="demo", user="u",
    )

    seen = [
        entry.get("file_name")
        for bucket in ("updated", "unchanged", "deleted")
        for entry in status[bucket]
    ]
    assert "theirs.py" not in seen
    assert status["errors"] == []


@pytest.mark.asyncio
async def test_sibling_deletion_is_not_reported_as_ours(settings, tmp_path):
    """The loudest symptom: a warning about a file this task never attached."""
    gone = tmp_path / "gone.py"  # never created

    prismind = AsyncMock()
    prismind.search_knowledge = AsyncMock(return_value=[
        _attachment(THEIRS, gone, "deadbeef"),
    ])

    status = await task._refresh_task_attachments(
        settings=settings, prismind=prismind, utid=MINE, project="demo", user="u",
    )

    assert status["deleted"] == []


@pytest.mark.asyncio
async def test_sibling_change_is_not_re_recorded_under_our_utid(settings, tmp_path):
    """The damaging symptom: the refresh writes, not just reads.

    A sibling whose file changed would be summarised again and stored with
    tags/source built from our UTID, while its content still names the
    other task.
    """
    theirs = tmp_path / "theirs.py"
    theirs.write_text("changed since it was recorded")

    prismind = AsyncMock()
    prismind.search_knowledge = AsyncMock(return_value=[
        _attachment(THEIRS, theirs, "stale-hash-so-it-looks-changed"),
    ])
    prismind.add_knowledge = AsyncMock(return_value={"success": True})

    summarise = AsyncMock()
    with patch.object(task, "_attachment_summary", summarise):
        status = await task._refresh_task_attachments(
            settings=settings, prismind=prismind, utid=MINE, project="demo", user="u",
        )

    assert status["updated"] == []
    # Nothing was re-summarised and nothing was written back. (The adapter
    # object itself is built before the loop, so its construction proves
    # nothing -- the calls are what would have caused the damage.)
    summarise.assert_not_awaited()
    prismind.add_knowledge.assert_not_awaited()
    prismind.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_our_own_attachment_still_passes(settings, tmp_path):
    """The filter must not swallow the case it exists to protect."""
    mine = tmp_path / "mine.py"
    mine.write_text("mine")

    prismind = AsyncMock()
    prismind.search_knowledge = AsyncMock(return_value=[
        _attachment(MINE, mine, task._calculate_file_hash(mine.read_bytes())),
    ])

    status = await task._refresh_task_attachments(
        settings=settings, prismind=prismind, utid=MINE, project="demo", user="u",
    )

    assert [e["file_name"] for e in status["unchanged"]] == ["mine.py"]


@pytest.mark.asyncio
async def test_unparseable_and_non_dict_entries_are_skipped(settings):
    """json.loads can yield a string or a list, not just raise."""
    prismind = AsyncMock()
    prismind.search_knowledge = AsyncMock(return_value=[
        {"id": "k-1", "content": "not json"},
        {"id": "k-2", "content": None},
        {"id": "k-3", "content": json.dumps(["a", "list"])},
        {"id": "k-4", "content": json.dumps("a string")},
    ])

    status = await task._refresh_task_attachments(
        settings=settings, prismind=prismind, utid=MINE, project="demo", user="u",
    )

    assert status == {"updated": [], "unchanged": [], "deleted": [], "errors": []}
