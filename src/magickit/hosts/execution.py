"""Run a fixed argv, without a shell, and stream its stdout line by line.

Every probe that needs a command goes through :func:`stream_lines`. The
argv is built by the probe from registry values and constants; nothing a
caller sends is spliced into it except bounded integers. stderr is
discarded rather than returned: it is free text from the tool and can carry
anything (an unknown unit's name, a path), and nothing a probe reports
depends on it.

The environment is replaced, not inherited: the MCP service's environment
holds the GitHub PATs on the authenticated instance, and a child has no
business seeing them.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field

#: What a probe's child process gets instead of our environment.
CHILD_ENV = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "SYSTEMD_PAGER": "",
    "SYSTEMD_COLORS": "0",
    "SYSTEMD_LESS": "",
}


class ProbeExecError(Exception):
    """The command could not be run, or did not finish in time."""


@dataclass
class StreamStats:
    """What happened while streaming (filled in as lines are read)."""

    lines: int = 0
    long_lines_skipped: int = 0
    truncated: bool = False
    returncode: int | None = None
    timed_out: bool = False
    extra: dict[str, int] = field(default_factory=dict)


async def stream_lines(
    argv: Sequence[str],
    *,
    timeout_s: float,
    max_lines: int,
    max_line_bytes: int,
    stats: StreamStats,
    env: Mapping[str, str] | None = None,
) -> AsyncIterator[bytes]:
    """Yield stdout lines of ``argv`` (without the newline).

    Stops early -- and kills the child -- after ``max_lines`` lines or
    ``timeout_s`` seconds; ``stats`` says which. A line longer than
    ``max_line_bytes`` is skipped and counted, never yielded: none of the
    outputs a probe parses has a legitimate line that long, so a long line
    is itself a sign of input the parser should not see. ``env`` replaces
    :data:`CHILD_ENV` for commands that need more (git); it is never the
    service's own environment.

    Raises:
        ProbeExecError: the command could not be started.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=dict(CHILD_ENV if env is None else env),
        )
    except OSError as exc:
        raise ProbeExecError(f"could not start {argv[0]}: {type(exc).__name__}") from None
    assert proc.stdout is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    buf = bytearray()
    #: True while inside an over-long line: everything up to the next
    #: newline belongs to it and is dropped. Splitting here rather than
    #: using ``StreamReader.readline`` matters: past its limit, readline
    #: clears its buffer and later returns the *tail* of the long line as
    #: if it were a line of its own.
    discarding = False
    try:
        done = False
        while not done:
            remaining = deadline - loop.time()
            if remaining <= 0:
                stats.timed_out = True
                break
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(65536), timeout=remaining)
            except TimeoutError:
                stats.timed_out = True
                break
            eof = not chunk
            buf.extend(chunk)
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    if len(buf) > max_line_bytes and not discarding:
                        discarding = True
                        stats.long_lines_skipped += 1
                    if discarding:
                        buf.clear()
                    break
                line = bytes(buf[:nl])
                del buf[: nl + 1]
                if discarding:
                    discarding = False
                    continue
                if len(line) > max_line_bytes:
                    stats.long_lines_skipped += 1
                    continue
                stats.lines += 1
                yield line
                if stats.lines >= max_lines:
                    stats.truncated = True
                    done = True
                    break
            if eof:
                # A final line without a newline is a truncated record:
                # nothing a probe parses ends that way, so it is dropped.
                break
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            stats.returncode = await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            stats.returncode = None


async def run_lines(
    argv: Sequence[str],
    *,
    timeout_s: float,
    max_lines: int,
    max_line_bytes: int = 4096,
    env: Mapping[str, str] | None = None,
) -> tuple[list[bytes], StreamStats]:
    """:func:`stream_lines`, collected. For commands with small output."""
    stats = StreamStats()
    lines = [
        line
        async for line in stream_lines(
            argv,
            timeout_s=timeout_s,
            max_lines=max_lines,
            max_line_bytes=max_line_bytes,
            stats=stats,
            env=env,
        )
    ]
    return lines, stats
