"""Streaming a probe's command: bounded, no shell, no inherited environment."""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX subprocess semantics")

from magickit.hosts import execution as ex  # noqa: E402


def py(code: str) -> list[str]:
    return [sys.executable, "-I", "-c", code]


async def test_long_lines_are_skipped_whole_not_returned_as_fragments():
    # The second long line is longer than one read chunk, so its tail would
    # arrive as a "line" of its own under StreamReader.readline.
    code = (
        "import sys;"
        "sys.stdout.write('A'*10000+'\\n');"
        "sys.stdout.write('ok\\n');"
        "sys.stdout.write('B'*200000+'TAIL\\n');"
        "sys.stdout.write('fin\\n')"
    )
    lines, stats = await ex.run_lines(py(code), timeout_s=10, max_lines=100, max_line_bytes=4096)
    assert lines == [b"ok", b"fin"]
    assert stats.long_lines_skipped == 2


async def test_a_final_line_without_newline_is_dropped():
    lines, _ = await ex.run_lines(
        py("print('a'); import sys; sys.stdout.write('partial')"), timeout_s=10, max_lines=10
    )
    assert lines == [b"a"]


async def test_line_cap_stops_and_kills_the_child():
    lines, stats = await ex.run_lines(
        py("import itertools\nfor i in itertools.count(): print(i, flush=True)"),
        timeout_s=10,
        max_lines=5,
    )
    assert len(lines) == 5 and stats.truncated
    assert stats.returncode is not None


async def test_timeout_stops_and_kills_the_child():
    lines, stats = await ex.run_lines(py("import time; time.sleep(30)"), timeout_s=0.5, max_lines=5)
    assert lines == [] and stats.timed_out


async def test_the_service_environment_is_not_inherited(monkeypatch):
    monkeypatch.setenv("GITHUB_MCP_PAT_IMPLEMENTER", "github_pat_should_not_leak")
    lines, _ = await ex.run_lines(
        py("import os; [print(k) for k in sorted(os.environ)]"), timeout_s=10, max_lines=100
    )
    names = {line.decode() for line in lines}
    assert "GITHUB_MCP_PAT_IMPLEMENTER" not in names
    assert names <= set(ex.CHILD_ENV) | {"LC_CTYPE"}


async def test_arguments_are_not_shell_interpreted(tmp_path):
    marker = tmp_path / "pwned"
    lines, _ = await ex.run_lines(
        [sys.executable, "-I", "-c", "import sys; print(sys.argv[1])", f"$(touch {marker})"],
        timeout_s=10,
        max_lines=5,
    )
    assert lines == [f"$(touch {marker})".encode()]
    assert not marker.exists()


async def test_a_missing_command_is_a_probe_exec_error():
    with pytest.raises(ex.ProbeExecError):
        await ex.run_lines(["/nonexistent/binary"], timeout_s=1, max_lines=1)
