"""The probe catalogue: typed, read-only looks at a registered host.

R-1 lets an investigation run without approval only where the system can
confirm it only reads. Here that is confirmed by reading this file: each
probe is a fixed function, its arguments are registry names or bounded
integers (:class:`ParamSpec`), and what it runs is a fixed argv
(:mod:`.execution`), a ``/proc`` file, a registered file opened without
following links (:mod:`.fs`) or a loopback health URL. Adding a probe is a
pull request. A request that fits no probe is the signal to add one, not a
reason for a free-text door (msg-1757 A-2: v1 has none).

What is not here, and why:

- ``systemctl status`` (msg-1757 B-1): it prints child argv and the journal
  tail. :data:`UNIT_PROPERTIES` is a fixed ``-p`` list instead; ``show``
  without ``-p`` would print ``Environment=``.
- journal ``MESSAGE`` and log-file tails (ruling 2, msg-1770).
- a journal ``grep`` argument: with bodies withheld, a match filter would
  turn match counts into an oracle on the bodies -- one regex at a time.
- paths, addresses and hostnames in output: probe output is agent context,
  and agent context reaches chatroom posts and PR bodies.

Every value returned is rebuilt by :mod:`.fields`; values that fail are
dropped and counted under ``omitted``. :func:`run_probe` then passes the
result through :func:`.secretscan.scrub` as a last check.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from magickit.deploy import registry as deploy_registry
from magickit.hosts import execution as ex
from magickit.hosts import fields, fs, secretscan
from magickit.hosts.limits import RateLimiter
from magickit.hosts.registry import (
    Host,
    HostRegistryError,
    HostTarget,
    SelfTargetRefusedError,
    UnknownFileError,
    UnknownHostError,
    UnknownTargetError,
    all_hosts,
    resolve_file,
    resolve_host,
    resolve_target,
)
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

#: The largest response a probe may return, serialised.
MAX_RESPONSE_BYTES = 64 * 1024
#: Registered files larger than this are refused (env files are < 1 KiB).
MAX_FILE_BYTES = 64 * 1024

# -- unit_state --------------------------------------------------------------

#: The only properties ``systemctl show`` is asked for (msg-1757 B-1, M1:
#: measured 2026-11-25 on every registered unit -- no Environment, no path,
#: no argv in the output).
UNIT_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "Result",
    "MainPID",
    "NRestarts",
    "ExecMainCode",
    "ExecMainStatus",
    "ExecMainStartTimestamp",
    "ActiveEnterTimestamp",
    "InactiveEnterTimestamp",
    "StateChangeTimestamp",
    "MemoryCurrent",
    "MemoryPeak",
    "CPUUsageNSec",
    "TasksCurrent",
)
_ACTIVE_STATES = frozenset(
    {
        "active",
        "reloading",
        "inactive",
        "failed",
        "activating",
        "deactivating",
        "maintenance",
        "refreshing",
    }
)
_LOAD_STATES = frozenset(
    {"stub", "loaded", "not-found", "bad-setting", "error", "merged", "masked"}
)
_UINT64_MAX = 2**64 - 1
_MAX_PID = 4194304
_TIMESTAMP_RE = re.compile(r"^@([0-9]{1,12})$")

# -- journal_summary ---------------------------------------------------------

#: The only fields journalctl is asked to print. ``MESSAGE`` is not among
#: them, so the body never reaches this process. ``-o json`` always adds
#: ``__CURSOR``, ``__REALTIME_TIMESTAMP``, ``__MONOTONIC_TIMESTAMP``,
#: ``_BOOT_ID`` and ``__SEQNUM*``; of those only the realtime timestamp is
#: read. The ``*UNIT`` fields are read only to check that every row belongs
#: to the unit asked about (M3), never returned.
JOURNAL_FIELDS = (
    "PRIORITY",
    "_PID",
    "MESSAGE_ID",
    "EXIT_STATUS",
    "_SYSTEMD_UNIT",
    "UNIT",
    "OBJECT_SYSTEMD_UNIT",
    "COREDUMP_UNIT",
)
#: Newest entries scanned per unit and call. The busiest registered unit
#: wrote ~30k entries in 52h (2026-11-25), ~10 per minute.
JOURNAL_SCAN_CAP = 20000
JOURNAL_TIMEOUT_S = 20.0
#: A JSON line with only the fields above is ~450 bytes. Anything far
#: longer carries something a parser here should not read.
JOURNAL_MAX_LINE_BYTES = 4096
JOURNAL_MAX_HOURS = 72

# -- resources ---------------------------------------------------------------

_MEMINFO_KEYS = ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree")
_LOADAVG_RE = re.compile(r"^[0-9]{1,4}\.[0-9]{2}$")
_GPU_NAME_RE = re.compile(r"^[A-Za-z0-9 ._()-]{1,64}$")
GPU_QUERY = "index,name,memory.used,memory.total,utilization.gpu,temperature.gpu"

# -- health ------------------------------------------------------------------

#: lexora's /health is known to stall 20-30s; the deploy runner uses the same.
HEALTH_TIMEOUT_S = 30.0
_HTTP_ERRORS = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "RemoteProtocolError",
        "ReadError",
    }
)


class ProbeError(Exception):
    """A probe could not produce an answer. ``error_type`` is for the caller."""

    def __init__(self, error_type: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.extra = extra


# -- the seam to the host ------------------------------------------------------


class HostIO:
    """Everything a probe touches on the host, in one replaceable place.

    Tests substitute a subclass; nothing else in this module reaches the
    host. Every method takes values the probe built from the registry.
    """

    async def run_lines(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        max_lines: int,
        max_line_bytes: int = 4096,
        env: dict[str, str] | None = None,
    ) -> tuple[list[bytes], ex.StreamStats]:
        return await ex.run_lines(
            argv, timeout_s=timeout_s, max_lines=max_lines, max_line_bytes=max_line_bytes, env=env
        )

    def stream_lines(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        max_lines: int,
        max_line_bytes: int,
        stats: ex.StreamStats,
    ):
        return ex.stream_lines(
            argv,
            timeout_s=timeout_s,
            max_lines=max_lines,
            max_line_bytes=max_line_bytes,
            stats=stats,
        )

    async def read_registered(self, path: Any, *, max_bytes: int) -> fs.FileRead:
        return await asyncio.to_thread(fs.read_registered, path, max_bytes=max_bytes)

    async def read_proc(self, name: str) -> str:
        """``/proc/<name>`` for a fixed name (``meminfo``, ``loadavg``, ``net/tcp``)."""

        def _read() -> str:
            with open(f"/proc/{name}", encoding="ascii", errors="replace") as fh:
                return fh.read(1 << 20)

        return await asyncio.to_thread(_read)

    async def statvfs(self, path: str) -> os.statvfs_result:
        return await asyncio.to_thread(os.statvfs, path)

    async def http_status(self, url: str, *, timeout_s: float) -> tuple[int | None, str | None]:
        """Status code of ``url`` (headers only), or the exception's class name."""
        import httpx

        try:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                async with client.stream("GET", url, timeout=timeout_s) as response:
                    return response.status_code, None
        except httpx.HTTPError as exc:
            return None, type(exc).__name__

    def git_head(self, repo: Path) -> tuple[list[str], dict[str, str]]:
        """argv and environment for reading ``repo``'s HEAD."""
        env = dict(ex.CHILD_ENV)
        # No HOME: git reads neither the global nor the system config.
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
        return ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"], env

    def live_slot(self, releases_root: Path) -> str | None:
        from magickit.deploy import releases

        try:
            return releases.resolve(releases_root).current
        except releases.ReleaseLayoutError:
            return None

    def now(self) -> float:
        return time.time()


# -- parameters ----------------------------------------------------------------


@dataclass(frozen=True)
class ParamSpec:
    """One probe parameter: a bounded integer, or a registered file name."""

    name: str
    kind: str  # "int" | "file"
    default: int | None = None
    lo: int = 0
    hi: int = 0
    doc: str = ""


@dataclass
class ProbeContext:
    """What a probe function receives."""

    host: Host
    target: HostTarget | None
    params: dict[str, Any]
    io: HostIO
    omitted: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True)
class Probe:
    """A catalogue entry."""

    name: str
    summary: str
    needs_target: bool
    params: tuple[ParamSpec, ...]
    run: Callable[[ProbeContext], Awaitable[dict[str, Any]]]


def _iso(seconds: float) -> str:
    return dt.datetime.fromtimestamp(seconds, dt.UTC).isoformat(timespec="seconds")


# -- probes ----------------------------------------------------------------------


def unit_show_argv(unit: str) -> list[str]:
    """The one ``systemctl`` invocation this module makes."""
    return [
        "systemctl",
        "show",
        "--timestamp=unix",
        "-p",
        ",".join(UNIT_PROPERTIES),
        "--",
        unit,
    ]


def parse_unit_show(lines: Sequence[bytes], omitted: Counter[str]) -> dict[str, Any]:
    """Rebuild ``systemctl show`` output from validated values."""
    raw: dict[str, str] = {}
    for line in lines:
        text = line.decode("utf-8", "replace")
        key, sep, value = text.partition("=")
        if sep and key in UNIT_PROPERTIES and key not in raw:
            raw[key] = value
    out: dict[str, Any] = {}

    def put(name: str, value: Any, *, unset_ok: bool = True) -> None:
        source = raw.get(name)
        if value is None and not (unset_ok and source in (None, "", "[not set]")):
            omitted[f"unit_state.{name}"] += 1
        out[name] = value

    put("LoadState", fields.one_of(raw.get("LoadState"), _LOAD_STATES))
    put("ActiveState", fields.one_of(raw.get("ActiveState"), _ACTIVE_STATES))
    for name in ("SubState", "UnitFileState", "Result"):
        put(name, fields.slug(raw.get(name)))
    put("MainPID", fields.int_in(raw.get("MainPID"), 0, _MAX_PID))
    put("NRestarts", fields.int_in(raw.get("NRestarts"), 0, 10**9))
    put("ExecMainCode", fields.int_in(raw.get("ExecMainCode"), 0, 6))
    put("ExecMainStatus", fields.int_in(raw.get("ExecMainStatus"), 0, 255))
    for name in ("MemoryCurrent", "MemoryPeak", "CPUUsageNSec"):
        n = fields.int_in(raw.get(name), 0, _UINT64_MAX)
        if n == _UINT64_MAX:
            n = None  # systemd's "not available"
            raw[name] = "[not set]"
        put(name, n)
    put("TasksCurrent", fields.int_in(raw.get("TasksCurrent"), 0, 10**7))
    for name in (
        "ExecMainStartTimestamp",
        "ActiveEnterTimestamp",
        "InactiveEnterTimestamp",
        "StateChangeTimestamp",
    ):
        m = _TIMESTAMP_RE.match(raw.get(name) or "")
        put(name, _iso(int(m.group(1))) if m else None)
    return out


async def _unit_state(ctx: ProbeContext) -> dict[str, Any]:
    assert ctx.target is not None
    units = []
    for unit in ctx.target.units:
        lines, stats = await ctx.io.run_lines(unit_show_argv(unit), timeout_s=10.0, max_lines=64)
        if stats.timed_out or stats.returncode != 0:
            raise ProbeError(
                "probe_failed", f"systemctl show did not succeed for a unit of {ctx.target.name}"
            )
        units.append({"unit": unit, **parse_unit_show(lines, ctx.omitted)})
    return {"units": units}


def journal_argv(selector: Sequence[str], *, since_s: int, until_s: int, cap: int) -> list[str]:
    """The journalctl invocation, minus how the unit is selected.

    The probe always passes ``["-u", <registered unit>]``; the host test in
    ``tests/host/`` passes a ``--user-unit`` of its own transient unit, so
    that it exercises exactly this argv and :func:`parse_journal`.
    """
    return [
        "journalctl",
        "--no-pager",
        "-q",
        "-o",
        "json",
        "--output-fields=" + ",".join(JOURNAL_FIELDS),
        "--since",
        f"@{since_s}",
        "--until",
        f"@{until_s}",
        "-r",
        "-n",
        str(cap),
        *selector,
    ]


@dataclass
class JournalSummary:
    """The rebuilt journal view of one unit."""

    scanned: int = 0
    by_priority: Counter[str] = field(default_factory=Counter)
    by_hour: Counter[int] = field(default_factory=Counter)
    entries: list[dict[str, Any]] = field(default_factory=list)
    oldest_us: int | None = None
    newest_us: int | None = None


def parse_journal_line(
    line: bytes,
    *,
    unit: str,
    since_us: int,
    until_us: int,
    omitted: Counter[str],
) -> dict[str, Any] | None:
    """One journal JSON line -> a rebuilt entry, or None (dropped, counted).

    Nothing from the line is passed through: each returned value is a
    bounded integer, a 32-hex id, or an ISO timestamp computed here from a
    validated integer (msg-1772 Obj-2-2). The unit is not read from the
    line either -- it is the registry name the caller asked for.
    """
    try:
        row = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        omitted["journal.malformed_line"] += 1
        return None
    if not isinstance(row, dict):
        omitted["journal.malformed_line"] += 1
        return None
    if unit not in (
        row.get("_SYSTEMD_UNIT"),
        row.get("UNIT"),
        row.get("OBJECT_SYSTEMD_UNIT"),
        row.get("COREDUMP_UNIT"),
    ):
        omitted["journal.foreign_row"] += 1
        return None
    ts = fields.int_in(row.get("__REALTIME_TIMESTAMP"), since_us, until_us)
    if ts is None:
        omitted["journal.bad_timestamp"] += 1
        return None
    entry: dict[str, Any] = {"ts_us": ts}
    for key, name, check in (
        ("PRIORITY", "priority", lambda v: fields.int_in(v, 0, 7)),
        ("_PID", "pid", lambda v: fields.int_in(v, 1, _MAX_PID)),
        ("MESSAGE_ID", "message_id", fields.hex32),
        ("EXIT_STATUS", "exit_status", lambda v: fields.int_in(v, 0, 255)),
    ):
        if key not in row:
            continue
        value = check(row[key])
        if value is None:
            omitted[f"journal.{name}"] += 1
        else:
            entry[name] = value
    return entry


async def summarise_journal(
    io: HostIO,
    selector: Sequence[str],
    *,
    unit: str,
    since_s: int,
    until_s: int,
    limit: int,
    omitted: Counter[str],
    cap: int = JOURNAL_SCAN_CAP,
) -> tuple[JournalSummary, ex.StreamStats]:
    """Stream journalctl for one unit and fold it into a :class:`JournalSummary`."""
    summary = JournalSummary()
    stats = ex.StreamStats()
    since_us, until_us = since_s * 1_000_000, (until_s + 1) * 1_000_000
    async for line in io.stream_lines(
        journal_argv(selector, since_s=since_s, until_s=until_s, cap=cap),
        timeout_s=JOURNAL_TIMEOUT_S,
        max_lines=cap,
        max_line_bytes=JOURNAL_MAX_LINE_BYTES,
        stats=stats,
    ):
        entry = parse_journal_line(
            line, unit=unit, since_us=since_us, until_us=until_us, omitted=omitted
        )
        if entry is None:
            continue
        summary.scanned += 1
        ts = entry["ts_us"]
        summary.newest_us = ts if summary.newest_us is None else max(summary.newest_us, ts)
        summary.oldest_us = ts if summary.oldest_us is None else min(summary.oldest_us, ts)
        summary.by_priority[str(entry.get("priority", "unset"))] += 1
        summary.by_hour[ts // 3_600_000_000] += 1
        if len(summary.entries) < limit:
            summary.entries.append(entry)
    if stats.long_lines_skipped:
        omitted["journal.long_line"] += stats.long_lines_skipped
    return summary, stats


async def _journal_summary(ctx: ProbeContext) -> dict[str, Any]:
    assert ctx.target is not None
    until_s = int(ctx.io.now())
    since_s = until_s - ctx.params["since_minutes"] * 60
    limit = ctx.params["limit"]
    units = []
    for unit in ctx.target.units:
        summary, stats = await summarise_journal(
            ctx.io,
            ["-u", unit],
            unit=unit,
            since_s=since_s,
            until_s=until_s,
            limit=limit,
            omitted=ctx.omitted,
        )
        if stats.returncode not in (0, None) and summary.scanned == 0:
            raise ProbeError(
                "probe_failed", f"journalctl did not succeed for a unit of {ctx.target.name}"
            )
        entries = []
        for e in summary.entries:
            item = {"ts": _iso(e["ts_us"] / 1e6)}
            item.update({k: v for k, v in e.items() if k != "ts_us"})
            entries.append(item)
        units.append(
            {
                "unit": unit,
                "since": _iso(since_s),
                "until": _iso(until_s),
                "scanned": summary.scanned,
                # True when the scan stopped at the cap or the timeout: the
                # counts then cover only the newest part of the window,
                # from `oldest_scanned` on.
                "partial": stats.truncated or stats.timed_out,
                "oldest_scanned": _iso(summary.oldest_us / 1e6) if summary.oldest_us else None,
                "counts_by_priority": dict(sorted(summary.by_priority.items())),
                "counts_by_hour": [
                    {"hour": _iso(h * 3600), "count": n} for h, n in sorted(summary.by_hour.items())
                ],
                "entries": entries,
            }
        )
    return {"units": units, "fields": ["ts", "priority", "pid", "message_id", "exit_status"]}


_ASSIGN_RE = re.compile(r"^(?:export\s+)?([^=\s]+)\s*=(.*)$")


def parse_env_keys(data: bytes, omitted: Counter[str]) -> dict[str, Any]:
    """Key names of an env file. Values are split off and dropped unread.

    Quoted values that continue over lines, and backslash continuations,
    are skipped as continuations: otherwise a ``foo=bar`` inside a
    multi-line value would come back as a key name -- that is, part of a
    value would come back.
    """
    text = data.decode("utf-8", "replace")
    keys: list[str] = []
    seen: set[str] = set()
    duplicates = 0
    non_key = 0
    continuation = 0
    open_quote: str | None = None
    backslash = False
    for line in text.splitlines():
        if open_quote is not None:
            continuation += 1
            if _closes(line, open_quote):
                open_quote = None
            continue
        if backslash:
            continuation += 1
            backslash = line.endswith("\\")
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _ASSIGN_RE.match(stripped)
        if not m:
            non_key += 1
            continue
        key = fields.env_key(m.group(1))
        value = m.group(2).lstrip()
        if value[:1] in ("'", '"') and not _closes(value[1:], value[0]):
            open_quote = value[0]
        elif value.endswith("\\"):
            backslash = True
        if key is None:
            non_key += 1
            continue
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        keys.append(key)
    if non_key:
        omitted["env_keys.non_key_line"] += non_key
    return {
        "keys": sorted(keys),
        "duplicate_keys": duplicates,
        "continuation_lines": continuation,
        "unterminated_quote": open_quote is not None,
    }


def _closes(text: str, quote: str) -> bool:
    """Does ``text`` contain an unescaped ``quote``?"""
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
        elif ch == "\\" and quote == '"':
            escaped = True
        elif ch == quote:
            return True
    return False


async def _env_keys(ctx: ProbeContext) -> dict[str, Any]:
    assert ctx.target is not None
    env = resolve_file(ctx.target, ctx.params["file"])
    try:
        read = await ctx.io.read_registered(env.path, max_bytes=MAX_FILE_BYTES)
    except fs.UnsafePathError as exc:
        raise ProbeError("unsafe_path", f"{ctx.target.name}/{env.name}: {exc}") from None
    except OSError as exc:
        raise ProbeError(
            "probe_failed", f"{ctx.target.name}/{env.name}: {type(exc).__name__}"
        ) from None
    result = parse_env_keys(read.data, ctx.omitted)
    return {
        "file": env.name,
        **result,
        # Step 2 (config changes) fills this from the per-key type allowlist
        # (msg-1759 Obj-1-2). Until then no key is changeable.
        "changeable_keys": {},
    }


async def _deployed_sha(ctx: ProbeContext) -> dict[str, Any]:
    assert ctx.target is not None
    if ctx.target.deploy_target is None:
        raise ProbeError(
            "not_applicable",
            f"{ctx.target.name} is not a deploy target, so it has no deployed sha",
        )
    d = deploy_registry.resolve_target(ctx.target.deploy_target)
    argv, env = ctx.io.git_head(d.repo_path)
    lines, stats = await ctx.io.run_lines(argv, timeout_s=10.0, max_lines=2, env=env)
    sha = fields.sha40(lines[0].decode("ascii", "replace")) if lines else None
    if stats.returncode != 0 or sha is None:
        raise ProbeError("probe_failed", f"could not read the commit of {ctx.target.name}")
    slot = None
    if d.releases_root is not None:
        slot = fields.one_of(ctx.io.live_slot(d.releases_root), ("a", "b"))
        if slot is None:
            ctx.omitted["deployed_sha.live_slot"] += 1
    return {"sha": sha, "live_slot": slot}


async def _health(ctx: ProbeContext) -> dict[str, Any]:
    assert ctx.target is not None
    if ctx.target.health_url is None:
        raise ProbeError("not_applicable", f"{ctx.target.name} declares no health endpoint")
    started = time.monotonic()
    status, error = await ctx.io.http_status(ctx.target.health_url, timeout_s=HEALTH_TIMEOUT_S)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    code = fields.int_in(status, 100, 599) if status is not None else None
    return {
        "status_code": code,
        "ok": code is not None and code < 400,
        "error": (error if error in _HTTP_ERRORS else "HTTPError") if error else None,
        "elapsed_ms": elapsed_ms,
    }


def parse_listening_ports(text: str) -> set[int]:
    """Local ports in LISTEN state from ``/proc/net/tcp`` or ``tcp6``."""
    ports: set[int] = set()
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 4 or cols[3] != "0A":
            continue
        _, _, port_hex = cols[1].rpartition(":")
        if re.fullmatch(r"[0-9A-F]{4}", port_hex):
            ports.add(int(port_hex, 16))
    return ports


async def _listening(ctx: ProbeContext) -> dict[str, Any]:
    assert ctx.target is not None
    listening: set[int] = set()
    for name in ("net/tcp", "net/tcp6"):
        try:
            listening |= parse_listening_ports(await ctx.io.read_proc(name))
        except OSError:
            ctx.omitted[f"listening.{name}"] += 1
    return {"ports": [{"port": p, "listening": p in listening} for p in ctx.target.ports]}


def parse_meminfo(text: str) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in _MEMINFO_KEYS:
            parts = rest.split()
            kib = fields.int_in(parts[0], 0, 2**50) if parts else None
            out[key] = kib * 1024 if kib is not None else None
    return {k: out.get(k) for k in _MEMINFO_KEYS}


def parse_gpus(lines: Sequence[bytes], omitted: Counter[str]) -> list[dict[str, Any]]:
    gpus = []
    for line in lines[:16]:
        cols = [c.strip() for c in line.decode("utf-8", "replace").split(",")]
        if len(cols) != 6:
            omitted["resources.gpu_line"] += 1
            continue
        name = cols[1] if _GPU_NAME_RE.match(cols[1]) else None
        if name is None:
            omitted["resources.gpu_name"] += 1
        gpus.append(
            {
                "index": fields.int_in(cols[0], 0, 64),
                "name": name,
                "memory_used_mib": fields.int_in(cols[2], 0, 10**7),
                "memory_total_mib": fields.int_in(cols[3], 0, 10**7),
                "utilization_pct": fields.int_in(cols[4], 0, 100),
                "temperature_c": fields.int_in(cols[5], 0, 200),
            }
        )
    return gpus


async def _resources(ctx: ProbeContext) -> dict[str, Any]:
    out: dict[str, Any] = {"cpu_count": os.cpu_count()}
    try:
        out["memory_bytes"] = parse_meminfo(await ctx.io.read_proc("meminfo"))
    except OSError:
        out["memory_bytes"] = None
        ctx.omitted["resources.meminfo"] += 1
    try:
        parts = (await ctx.io.read_proc("loadavg")).split()[:3]
        out["loadavg"] = [float(p) if _LOADAVG_RE.match(p) else None for p in parts]
    except OSError:
        out["loadavg"] = None
        ctx.omitted["resources.loadavg"] += 1
    disks = []
    for path in ctx.host.disk_paths:
        try:
            st = await ctx.io.statvfs(path)
            disks.append(
                {
                    "path": path,
                    "total_bytes": st.f_blocks * st.f_frsize,
                    "available_bytes": st.f_bavail * st.f_frsize,
                }
            )
        except OSError:
            ctx.omitted["resources.disk"] += 1
    out["disks"] = disks
    argv = [
        "nvidia-smi",
        f"--query-gpu={GPU_QUERY}",
        "--format=csv,noheader,nounits",
    ]
    try:
        lines, stats = await ctx.io.run_lines(
            argv, timeout_s=10.0, max_lines=16, max_line_bytes=512
        )
        out["gpus"] = parse_gpus(lines, ctx.omitted) if stats.returncode == 0 else None
    except ex.ProbeExecError:
        out["gpus"] = None
    return out


PROBES: dict[str, Probe] = {
    p.name: p
    for p in (
        Probe(
            name="unit_state",
            summary=(
                "systemd state of the target's units: active/sub/load state, result, "
                "main PID, restarts, exit code/status, start/state-change times, memory, "
                "CPU time, tasks. A fixed property list; never `status`."
            ),
            needs_target=True,
            params=(),
            run=_unit_state,
        ),
        Probe(
            name="journal_summary",
            summary=(
                "Structured journal fields of the target's units over the last "
                "`since_minutes`: counts by priority and by hour, and the newest `limit` "
                "entries as {ts, priority, pid, message_id, exit_status}. Message bodies "
                "are never returned."
            ),
            needs_target=True,
            params=(
                ParamSpec(
                    "since_minutes",
                    "int",
                    default=60,
                    lo=1,
                    hi=JOURNAL_MAX_HOURS * 60,
                    doc="window length in minutes",
                ),
                ParamSpec("limit", "int", default=50, lo=0, hi=200, doc="entries returned"),
            ),
            run=_journal_summary,
        ),
        Probe(
            name="env_keys",
            summary=(
                "Key names of a registered env file of the target (`file` = its logical "
                "name from host_task_targets). Values are never returned, not even "
                "redacted or hashed."
            ),
            needs_target=True,
            params=(ParamSpec("file", "file", doc="registered file name"),),
            run=_env_keys,
        ),
        Probe(
            name="deployed_sha",
            summary="The commit the target's live tree is on, and which release slot is live.",
            needs_target=True,
            params=(),
            run=_deployed_sha,
        ),
        Probe(
            name="health",
            summary="Status code of the target's loopback health endpoint (status line only).",
            needs_target=True,
            params=(),
            run=_health,
        ),
        Probe(
            name="listening",
            summary="Whether each port the target is registered with is in LISTEN state.",
            needs_target=True,
            params=(),
            run=_listening,
        ),
        Probe(
            name="resources",
            summary="Host-wide memory, load average, disk space of registered filesystems, GPUs.",
            needs_target=False,
            params=(),
            run=_resources,
        ),
    )
}


def _validate_params(probe: Probe, target: HostTarget | None, params: Any) -> dict[str, Any]:
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ProbeError("invalid_param", "params must be an object")
    allowed = {p.name: p for p in probe.params}
    unknown = sorted(str(k) for k in params if k not in allowed)
    if unknown:
        raise ProbeError(
            "unknown_param",
            f"{probe.name} takes {sorted(allowed) or 'no params'}; got {unknown}",
        )
    out: dict[str, Any] = {}
    for spec in probe.params:
        value = params.get(spec.name, spec.default)
        if spec.kind == "int":
            n = fields.int_in(value, spec.lo, spec.hi) if not isinstance(value, str) else None
            if n is None:
                raise ProbeError(
                    "invalid_param",
                    f"{spec.name} must be an integer in [{spec.lo}, {spec.hi}]",
                )
            out[spec.name] = n
        elif spec.kind == "file":
            if value is None:
                assert target is not None
                registered = sorted(target.env_files)
                raise ProbeError(
                    "invalid_param",
                    f"{spec.name} is required; registered on {target.name}: {registered or 'none'}",
                )
            out[spec.name] = value
    return out


def _fit(result: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Trim journal entries until the response fits :data:`MAX_RESPONSE_BYTES`."""
    trimmed = False
    while len(json.dumps(result, default=str)) > MAX_RESPONSE_BYTES:
        units = result.get("units") if isinstance(result, dict) else None
        biggest = None
        if isinstance(units, list):
            with_entries = [u for u in units if isinstance(u, dict) and u.get("entries")]
            if with_entries:
                biggest = max(with_entries, key=lambda u: len(u["entries"]))
        if biggest is None:
            raise ProbeError("output_too_large", "the result does not fit the response limit")
        biggest["entries"] = biggest["entries"][: len(biggest["entries"]) // 2]
        trimmed = True
    return result, trimmed


async def run_probe(
    host: str,
    probe: str,
    target: str,
    params: Any,
    *,
    limiter: RateLimiter,
    io: HostIO | None = None,
    hosts: Mapping[str, Host] | None = None,
) -> dict[str, Any]:
    """Run one probe and return the tool's response (never raises).

    Args:
        host: registered host name.
        probe: a name in :data:`PROBES`.
        target: registered target name ("" for host-wide probes).
        params: the probe's parameters.
        limiter: the per-process rate limiter.
        io: the host seam (tests substitute it).
        hosts: the registry table (tests substitute it).
    """
    started = time.monotonic()
    io = io or HostIO()
    outcome = "ok"
    try:
        try:
            h = resolve_host(host, hosts=hosts)
        except UnknownHostError as exc:
            raise ProbeError("unknown_host", str(exc)) from None
        spec = PROBES.get(probe) if isinstance(probe, str) else None
        if spec is None:
            raise ProbeError("unknown_probe", f"{probe!r} is not a probe. Probes: {sorted(PROBES)}")
        t: HostTarget | None = None
        if spec.needs_target:
            if not target:
                raise ProbeError(
                    "target_required",
                    f"{probe} needs a target; on {h.name}: {sorted(h.targets)}",
                )
            try:
                t = resolve_target(h, target)
            except SelfTargetRefusedError as exc:
                raise ProbeError("out_of_scope", str(exc)) from None
            except UnknownTargetError as exc:
                raise ProbeError("unknown_target", str(exc)) from None
        elif target:
            raise ProbeError("target_not_applicable", f"{probe} is host-wide; leave target empty")
        validated = _validate_params(spec, t, params)
        wait = limiter.admit(spec.name)
        if wait > 0:
            raise ProbeError(
                "rate_limited",
                f"too many probes; retry in {int(wait) + 1}s",
                retry_after_s=int(wait) + 1,
            )
        ctx = ProbeContext(host=h, target=t, params=validated, io=io)
        async with limiter.semaphore:
            try:
                result = await spec.run(ctx)
            except UnknownFileError as exc:
                raise ProbeError("unknown_file", str(exc)) from None
            except HostRegistryError as exc:
                raise ProbeError("unknown_target", str(exc)) from None
            except ex.ProbeExecError as exc:
                raise ProbeError("probe_failed", str(exc)) from None
        omitted = ctx.omitted
        result = secretscan.scrub(result, omitted)
        result, trimmed = _fit(result)
        response = {
            "ok": True,
            "host": h.name,
            "probe": spec.name,
            "target": t.name if t else None,
            "params": {k: v for k, v in validated.items()},
            "result": result,
            "omitted": dict(sorted(omitted.items())),
            "trimmed_to_fit": trimmed,
        }
        return response
    except ProbeError as exc:
        outcome = exc.error_type
        return {"ok": False, "error_type": exc.error_type, "message": str(exc), **exc.extra}
    except Exception as exc:  # a probe bug must not take the tool down
        outcome = "internal_error"
        logger.exception("host_probe failed", probe=probe)
        return {
            "ok": False,
            "error_type": "internal_error",
            "message": f"the probe failed unexpectedly ({type(exc).__name__})",
        }
    finally:
        # The audit line. Caller strings are cut short: on the no-auth
        # instance they are whatever anyone sent.
        logger.info(
            "host_probe",
            host=host[:64] if isinstance(host, str) else None,
            probe=probe[:64] if isinstance(probe, str) else None,
            target=target[:64] if isinstance(target, str) else None,
            outcome=outcome,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def describe_targets(hosts: Mapping[str, Host] | None = None) -> dict[str, Any]:
    """``host_task_targets``: hosts, their targets and the probe catalogue.

    Names only: no paths, addresses or health URLs. ``ports`` are listed
    because the ``listening`` probe reports on them, and port numbers alone
    point nowhere (platform:infra-registry §3).
    """
    table = all_hosts() if hosts is None else hosts
    out_hosts = []
    for h in sorted(table.values(), key=lambda x: x.name):
        targets = []
        for t in sorted(h.targets.values(), key=lambda x: x.name):
            targets.append(
                {
                    "name": t.name,
                    "units": list(t.units),
                    "ports": list(t.ports),
                    "has_health": t.health_url is not None,
                    "deployable": t.deploy_target is not None,
                    "files": [{"name": f.name, "format": "env"} for f in t.env_files.values()],
                }
            )
        out_hosts.append({"name": h.name, "kind": h.kind, "targets": targets})
    return {
        "ok": True,
        "hosts": out_hosts,
        "probes": [
            {
                "name": p.name,
                "summary": p.summary,
                "needs_target": p.needs_target,
                "params": [
                    {
                        "name": s.name,
                        "kind": s.kind,
                        **(
                            {"default": s.default, "min": s.lo, "max": s.hi}
                            if s.kind == "int"
                            else {}
                        ),
                        "doc": s.doc,
                    }
                    for s in p.params
                ],
            }
            for p in PROBES.values()
        ],
    }
