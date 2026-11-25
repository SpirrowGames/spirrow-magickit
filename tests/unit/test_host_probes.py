"""host_probe: fixed argv in, rebuilt values out, bodies never.

The fake host below is deliberately hostile: its journalctl ignores
``--output-fields`` and returns ``MESSAGE`` anyway, its env file has a
secret in every value, and its ``systemctl show`` adds ``Environment=``.
None of that may reach the response, because the response is rebuilt from
validated values rather than filtered.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest

from magickit.hosts import execution as ex
from magickit.hosts import fs, probes
from magickit.hosts.limits import RateLimiter, Window

SECRET = "hunter2-" + "Zq8" * 12
NOW = 1_795_600_000  # 2026-11-25


class FakeIO(probes.HostIO):
    """A host that tries to leak through every probe."""

    def __init__(self, *, journal: list[bytes] | None = None, env: bytes = b"") -> None:
        self.argvs: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []
        self.journal = journal or []
        self.env = env
        self.show = [
            b"LoadState=loaded",
            b"ActiveState=active",
            b"SubState=running",
            b"UnitFileState=enabled",
            b"Result=success",
            b"MainPID=1234",
            b"NRestarts=2",
            b"ExecMainCode=0",
            b"ExecMainStatus=0",
            b"ExecMainStartTimestamp=@1795337674",
            b"ActiveEnterTimestamp=@1795337674",
            b"InactiveEnterTimestamp=",
            b"StateChangeTimestamp=@1795337674",
            b"MemoryCurrent=139345920",
            b"MemoryPeak=[not set]",
            b"CPUUsageNSec=18446744073709551615",
            b"TasksCurrent=8",
            ("Environment=TOKEN=" + SECRET).encode(),
        ]
        self.http: tuple[int | None, str | None] = (200, None)
        self.git = [b"d48ebad77d9dab48c27dc7ca2b334e938811e4d1"]
        self.proc = {
            "meminfo": (
                "MemTotal: 1000 kB\nMemAvailable: 500 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"
            ),
            "loadavg": "0.09 0.26 0.46 1/2161 968710\n",
            "net/tcp": (
                "  sl  local_address rem_address   st\n"
                "   0: 0100007F:1FBB 00000000:0000 0A 00000000\n"
                "   1: 0100007F:1F90 00000000:0000 01 00000000\n"
            ),
            "net/tcp6": "  sl  local_address rem_address   st\n",
        }

    async def run_lines(self, argv, *, timeout_s, max_lines, max_line_bytes=4096, env=None):
        self.argvs.append(list(argv))
        self.envs.append(env)
        stats = ex.StreamStats(returncode=0)
        if argv[0] == "systemctl":
            return list(self.show), stats
        if argv[0] == "git":
            return list(self.git), stats
        if argv[0] == "nvidia-smi":
            return [b"0, NVIDIA GeForce RTX 5090, 31774, 32607, 0, 45"], stats
        raise AssertionError(argv)

    async def stream_lines(self, argv, *, timeout_s, max_lines, max_line_bytes, stats):
        self.argvs.append(list(argv))
        stats.returncode = 0
        for line in self.journal[:max_lines]:
            stats.lines += 1
            yield line

    async def read_registered(self, path, *, max_bytes):
        return fs.FileRead(data=self.env, size=len(self.env), mtime_ns=0)

    async def read_proc(self, name):
        return self.proc[name]

    async def statvfs(self, path):
        return SimpleNamespace(f_blocks=100, f_frsize=4096, f_bavail=40)

    async def http_status(self, url, *, timeout_s):
        return self.http

    def live_slot(self, releases_root: Path):
        return "b"

    def now(self) -> float:
        return float(NOW)


def jline(**fields: Any) -> bytes:
    row = {
        "__REALTIME_TIMESTAMP": str((NOW - 60) * 1_000_000),
        "__CURSOR": "s=abc;i=1",
        "_SYSTEMD_UNIT": "spirrow-lexora.service",
        "MESSAGE": "login with token=" + SECRET,
        "SYSLOG_IDENTIFIER": "start.sh",
        "_CMDLINE": "/usr/bin/python --api-key " + SECRET,
    }
    row.update(fields)
    return json.dumps({k: v for k, v in row.items() if v is not None}).encode()


def limiter() -> RateLimiter:
    return RateLimiter(global_windows=(Window(10_000, 60),), probe_windows={})


async def probe(io: FakeIO, name: str, target: str = "spirrow-lexora", params=None) -> dict:
    return await probes.run_probe("services", name, target, params, limiter=limiter(), io=io)


def assert_no_secret(response: dict) -> None:
    text = json.dumps(response)
    assert SECRET not in text
    assert "hunter2" not in text


# ── argv ─────────────────────────────────────────────────────────


def test_unit_show_argv_is_a_fixed_property_list():
    argv = probes.unit_show_argv("spirrow-lexora.service")
    assert argv[:3] == ["systemctl", "show", "--timestamp=unix"]
    assert argv[3] == "-p" and argv[4].split(",") == list(probes.UNIT_PROPERTIES)
    assert argv[-2:] == ["--", "spirrow-lexora.service"]
    assert "status" not in argv
    for prop in probes.UNIT_PROPERTIES:
        assert "Environment" not in prop and "Exec" not in prop.replace("ExecMain", "")


def test_journal_argv_never_asks_for_the_body():
    argv = probes.journal_argv(["-u", "x.service"], since_s=1, until_s=2, cap=10)
    fields_arg = next(a for a in argv if a.startswith("--output-fields="))
    requested = fields_arg.split("=", 1)[1].split(",")
    assert "MESSAGE" not in requested
    assert not any(f in requested for f in ("_CMDLINE", "SYSLOG_IDENTIFIER", "CODE_FILE"))
    assert argv[-2:] == ["-u", "x.service"]
    assert "-g" not in argv and "--grep" not in argv


# ── unit_state ───────────────────────────────────────────────────


async def test_unit_state_rebuilds_values_and_drops_environment():
    io = FakeIO()
    r = await probe(io, "unit_state")
    assert r["ok"], r
    unit = r["result"]["units"][0]
    assert unit["ActiveState"] == "active"
    assert unit["MainPID"] == 1234
    assert unit["MemoryPeak"] is None and unit["CPUUsageNSec"] is None  # unset, not omitted
    assert unit["InactiveEnterTimestamp"] is None
    assert unit["ExecMainStartTimestamp"].startswith("2026-11-22T")
    assert "Environment" not in unit
    assert r["omitted"] == {}
    assert_no_secret(r)


async def test_unit_state_omits_and_counts_forged_values():
    io = FakeIO()
    io.show = [
        b"ActiveState=active; token=" + SECRET.encode(),
        b"SubState=" + SECRET.encode(),
        b"MainPID=-1",
        b"ExecMainStatus=999",
        b"ExecMainStartTimestamp=Tue 2026-11-25",
    ]
    r = await probe(io, "unit_state")
    unit = r["result"]["units"][0]
    assert unit["ActiveState"] is None and unit["SubState"] is None
    assert unit["MainPID"] is None and unit["ExecMainStatus"] is None
    assert r["omitted"]["unit_state.ActiveState"] == 1
    assert r["omitted"]["unit_state.ExecMainStartTimestamp"] == 1
    assert_no_secret(r)


# ── journal_summary ──────────────────────────────────────────────


async def test_journal_summary_returns_only_validated_structured_fields():
    io = FakeIO(
        journal=[
            jline(PRIORITY="3", _PID="42", MESSAGE_ID="39f53479d3a045ac8e11786248231fbf"),
            jline(PRIORITY="6", _PID="42", EXIT_STATUS="1"),
        ]
    )
    r = await probe(io, "journal_summary", params={"since_minutes": 10, "limit": 5})
    assert r["ok"], r
    unit = r["result"]["units"][0]
    assert unit["unit"] == "spirrow-lexora.service"
    assert unit["scanned"] == 2
    assert unit["counts_by_priority"] == {"3": 1, "6": 1}
    e0, e1 = unit["entries"]
    assert set(e0) == {"ts", "priority", "pid", "message_id"}
    assert e1["exit_status"] == 1
    assert_no_secret(r)
    # the argv the fake saw is the real one
    assert io.argvs[0][-2:] == ["-u", "spirrow-lexora.service"]


@pytest.mark.parametrize(
    "forged,field",
    [
        ({"MESSAGE_ID": "token=" + SECRET}, "journal.message_id"),
        ({"MESSAGE_ID": SECRET}, "journal.message_id"),
        ({"MESSAGE_ID": "39F53479D3A045AC8E11786248231FBF"}, "journal.message_id"),
        ({"PRIORITY": "9"}, "journal.priority"),
        ({"PRIORITY": SECRET}, "journal.priority"),
        ({"EXIT_STATUS": "TERM"}, "journal.exit_status"),
        ({"EXIT_STATUS": "256"}, "journal.exit_status"),
        ({"_PID": "0"}, "journal.pid"),
        ({"PRIORITY": [54]}, "journal.priority"),  # byte-array form
        ({"MESSAGE_ID": ["39f53479d3a045ac8e11786248231fbf", "x"]}, "journal.message_id"),
    ],
)
async def test_journal_forged_fields_are_dropped_and_counted(forged, field):
    io = FakeIO(journal=[jline(**forged)])
    r = await probe(io, "journal_summary")
    assert r["ok"], r
    assert r["omitted"].get(field) == 1
    assert_no_secret(r)


async def test_journal_rows_of_other_units_and_out_of_window_are_dropped():
    io = FakeIO(
        journal=[
            jline(_SYSTEMD_UNIT="spirrow-magickit.service"),
            jline(__REALTIME_TIMESTAMP=str((NOW - 7200) * 1_000_000)),  # older than 60 min
            jline(__REALTIME_TIMESTAMP="not-a-number"),
            b"not json " + SECRET.encode(),
            b"[1, 2, 3]",
            jline(_SYSTEMD_UNIT=None, UNIT="spirrow-lexora.service", _PID="1"),  # systemd about it
        ]
    )
    r = await probe(io, "journal_summary")
    assert r["omitted"]["journal.foreign_row"] == 1
    assert r["omitted"]["journal.bad_timestamp"] == 2
    assert r["omitted"]["journal.malformed_line"] == 2
    assert r["result"]["units"][0]["scanned"] == 1
    assert_no_secret(r)


async def test_journal_limit_and_hourly_counts():
    io = FakeIO(
        journal=[jline(__REALTIME_TIMESTAMP=str((NOW - i * 600) * 1_000_000)) for i in range(1, 7)]
    )
    r = await probe(io, "journal_summary", params={"since_minutes": 120, "limit": 2})
    unit = r["result"]["units"][0]
    assert len(unit["entries"]) == 2
    assert sum(b["count"] for b in unit["counts_by_hour"]) == 6


@pytest.mark.parametrize(
    "params",
    [
        {"since_minutes": 0},
        {"since_minutes": 4321},
        {"since_minutes": "60"},
        {"since_minutes": True},
        {"since_minutes": 60.0},
        {"limit": 201},
    ],
)
async def test_journal_params_are_bounded_integers(params):
    r = await probe(FakeIO(), "journal_summary", params=params)
    assert r["error_type"] == "invalid_param"


@pytest.mark.parametrize("params", [{"grep": "token"}, {"unit": "ssh.service"}, {"path": "/etc"}])
async def test_unknown_params_are_refused(params):
    r = await probe(FakeIO(), "journal_summary", params=params)
    assert r["error_type"] == "unknown_param"


# ── env_keys ─────────────────────────────────────────────────────


ENV = f"""# comment with {SECRET}
DATABASE_URL=postgresql://app:{SECRET}@127.0.0.1/db
export LOG_LEVEL=info
PORT = 8115
MULTI="first line
SMUGGLED_{SECRET[:8].replace("-", "_")}=x
last line"
CONT=abc\\
NOT_A_KEY_{SECRET[:4]}=continued
lower_ok=1
1BAD=2
just text
DATABASE_URL=again
""".encode()


async def test_env_keys_returns_names_only():
    r = await probe(FakeIO(env=ENV), "env_keys", target="spirrow-conclair", params={"file": "env"})
    assert r["ok"], r
    res = r["result"]
    assert res["keys"] == ["CONT", "DATABASE_URL", "LOG_LEVEL", "MULTI", "PORT", "lower_ok"]
    assert res["duplicate_keys"] == 1
    assert res["continuation_lines"] == 3
    assert res["changeable_keys"] == {}
    assert r["omitted"]["env_keys.non_key_line"] == 2  # 1BAD, "just text"
    assert_no_secret(r)
    assert "SMUGGLED" not in json.dumps(r) and "NOT_A_KEY" not in json.dumps(r)


async def test_env_keys_needs_a_registered_file():
    r = await probe(FakeIO(), "env_keys", target="spirrow-conclair")
    assert r["error_type"] == "invalid_param"
    r = await probe(FakeIO(), "env_keys", target="spirrow-conclair", params={"file": "/etc/shadow"})
    assert r["error_type"] == "unknown_file"
    r = await probe(FakeIO(), "env_keys", target="spirrow-lexora", params={"file": "env"})
    assert r["error_type"] == "unknown_file"


async def test_env_keys_reports_unsafe_paths_without_the_path():
    class Unsafe(FakeIO):
        async def read_registered(self, path, *, max_bytes):
            raise fs.UnsafePathError("component 3 of the path is writable by an untrusted account")

    r = await probe(Unsafe(), "env_keys", target="spirrow-conclair", params={"file": "env"})
    assert r["error_type"] == "unsafe_path"
    assert "/home" not in r["message"] and "releases" not in r["message"]


# ── the rest ─────────────────────────────────────────────────────


async def test_deployed_sha_reads_head_without_user_or_system_git_config():
    io = FakeIO()
    r = await probe(io, "deployed_sha", target="spirrow-cognilens")
    assert r["result"] == {"sha": "d48ebad77d9dab48c27dc7ca2b334e938811e4d1", "live_slot": "b"}
    argv, env = io.argvs[0], io.envs[0]
    assert argv[0] == "git" and argv[-3:] == ["rev-parse", "--verify", "HEAD"]
    assert env is not None and env["GIT_CONFIG_NOSYSTEM"] == "1" and "HOME" not in env


async def test_deployed_sha_refuses_a_malformed_sha_and_non_deploy_targets():
    io = FakeIO()
    io.git = [SECRET.encode()]
    r = await probe(io, "deployed_sha", target="spirrow-cognilens")
    assert r["error_type"] == "probe_failed"
    assert_no_secret(r)
    r = await probe(FakeIO(), "deployed_sha", target="vllm-32b")
    assert r["error_type"] == "not_applicable"


async def test_health_reports_status_and_a_fixed_error_class():
    io = FakeIO()
    r = await probe(io, "health")
    assert r["result"]["status_code"] == 200 and r["result"]["ok"] is True
    io.http = (None, "SomethingCustom" + SECRET)
    r = await probe(io, "health")
    assert r["result"]["error"] == "HTTPError"
    assert_no_secret(r)


async def test_listening_reports_registered_ports_only():
    io = FakeIO()
    r = await probe(io, "listening", target="spirrow-lexora")  # 8110 = 0x1FAE
    assert r["result"] == {"ports": [{"port": 8110, "listening": False}]}
    io.proc["net/tcp"] = io.proc["net/tcp"].replace("1FBB", "1FAE")
    r = await probe(io, "listening", target="spirrow-lexora")
    assert r["result"] == {"ports": [{"port": 8110, "listening": True}]}


async def test_resources_is_host_wide():
    io = FakeIO()
    r = await probe(io, "resources", target="")
    assert r["ok"], r
    res = r["result"]
    assert res["memory_bytes"]["MemTotal"] == 1000 * 1024
    assert res["loadavg"] == [0.09, 0.26, 0.46]
    assert res["disks"] == [{"path": "/", "total_bytes": 409600, "available_bytes": 163840}]
    assert res["gpus"][0]["name"] == "NVIDIA GeForce RTX 5090"
    r = await probe(io, "resources", target="spirrow-lexora")
    assert r["error_type"] == "target_not_applicable"


def test_gpu_names_are_validated():
    omitted: Counter[str] = Counter()
    gpus = probes.parse_gpus([b"0, $(touch x), 1, 2, 3, 4", b"garbage"], omitted)
    assert gpus[0]["name"] is None
    assert omitted == Counter({"resources.gpu_name": 1, "resources.gpu_line": 1})


# ── the door ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host,name,target,error",
    [
        ("sg-example-01", "unit_state", "spirrow-lexora", "unknown_host"),
        ("services", "status", "spirrow-lexora", "unknown_probe"),
        ("services", "shell", "spirrow-lexora", "unknown_probe"),
        ("services", "unit_state", "", "target_required"),
        ("services", "unit_state", "ssh", "unknown_target"),
        ("services", "unit_state", "../../etc", "unknown_target"),
        ("services", "unit_state", "spirrow-magickit-mcp-local", "out_of_scope"),
    ],
)
async def test_refusals(host, name, target, error):
    r = await probes.run_probe(host, name, target, None, limiter=limiter(), io=FakeIO())
    assert r["ok"] is False and r["error_type"] == error


async def test_rate_limit_answers_with_retry_after():
    lim = RateLimiter(global_windows=(Window(1, 60),), probe_windows={})
    io = FakeIO()
    first = await probes.run_probe(
        "services", "unit_state", "spirrow-lexora", None, limiter=lim, io=io
    )
    second = await probes.run_probe(
        "services", "unit_state", "spirrow-lexora", None, limiter=lim, io=io
    )
    assert first["ok"] and second["error_type"] == "rate_limited"
    assert 1 <= second["retry_after_s"] <= 61


async def test_refused_calls_do_not_spend_the_rate_limit():
    lim = RateLimiter(global_windows=(Window(1, 60),), probe_windows={})
    io = FakeIO()
    await probes.run_probe("services", "nope", "", None, limiter=lim, io=io)
    ok = await probes.run_probe(
        "services", "unit_state", "spirrow-lexora", None, limiter=lim, io=io
    )
    assert ok["ok"]


async def test_the_final_scan_drops_a_secret_a_validator_let_through(monkeypatch):
    # Simulate a validator that is wider than it should be.
    monkeypatch.setattr(probes.fields, "slug", lambda v: v if isinstance(v, str) else None)
    io = FakeIO()
    io.show = [b"SubState=ghp_" + b"A" * 36]
    r = await probe(io, "unit_state")
    assert r["result"]["units"][0]["SubState"] is None
    assert r["omitted"]["secret_like:key"] == 1


async def test_big_results_are_trimmed_to_fit():
    io = FakeIO(journal=[jline(PRIORITY="6", _PID=str(i + 1)) for i in range(2000)])
    r = await probe(io, "journal_summary", params={"since_minutes": 60, "limit": 200})
    assert r["ok"]
    assert len(json.dumps(r)) <= probes.MAX_RESPONSE_BYTES + 512
    assert r["result"]["units"][0]["scanned"] == 2000


async def test_internal_errors_do_not_escape():
    class Broken(FakeIO):
        async def run_lines(self, *a, **k):
            raise RuntimeError(SECRET)

    r = await probe(Broken(), "unit_state")
    assert r["error_type"] == "internal_error"
    assert_no_secret(r)


def test_targets_listing_carries_no_paths_or_addresses():
    listing = probes.describe_targets()
    text = json.dumps(listing)
    for needle in ("/home", "/srv", "127.0.0.1", "http://", "sg-ai", "releases", ".env"):
        assert needle not in text, needle
    names = {p["name"] for p in listing["probes"]}
    assert names == set(probes.PROBES)


def test_probe_catalogue_has_no_free_text_parameter():
    for p in probes.PROBES.values():
        for spec in p.params:
            assert spec.kind in ("int", "file")


def test_env_file_path_is_a_posix_path():
    # The registry must keep working on the Windows loop host.
    from magickit.hosts import registry

    env = registry.resolve_file(
        registry.resolve_target(registry.resolve_host("services"), "spirrow-conclair"), "env"
    )
    assert isinstance(env.path, PurePosixPath) and env.path.is_absolute()
