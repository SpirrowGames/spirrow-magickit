"""Is the GPU quiet enough for unprompted work?

The digest sweeper is the only thing in Magickit that starts using the local
GPU with nobody asking, on the same card that serves the loop writing code.
``digest.sweeper_enabled`` answers "may it run at all"; this module answers
"may it run *now*", so the sweeper can be left on without becoming a standing
tax on every turn the loop takes.

**The signal is vLLM's queue, not the card's utilization.** ``nvidia-smi
--query-gpu=utilization.gpu`` is an instantaneous sample, and on this box it
reads 0% while 31,770 MiB of the 32,607 MiB card is resident weights. Neither
number answers the only question that matters -- is somebody waiting -- and
the memory figure never moves. vLLM publishes that question's answer directly::

    vllm:num_requests_running{engine="0",model_name="Qwen3.8-27B"} 0.0
    vllm:num_requests_waiting{engine="0",model_name="Qwen3.8-27B"} 0.0

and vLLM is the *only* process holding the card (``nvidia-smi
--query-compute-apps`` shows one ``VLLM::EngineCore`` and nothing else, since
Forge was stopped and bge-m3 moved to the CPU). So this gauge is the whole
picture of GPU demand rather than a piece of it, which is what makes gating on
it honest.

**Read from vLLM, not through Lexora.** Every model call in Magickit goes
through Lexora and that stays true -- but Lexora's ``/stats`` is cumulative
counters (``total_requests``, ``average_duration_seconds``), which cannot
answer "right now". This is an observability read, not a model call, so it
does not put Magickit around its own gateway. ``digest.gpu_metrics_url`` is
configurable so it can move the day Lexora grows an in-flight gauge.

**Asymmetric on purpose.** Entering a cycle requires sustained quiet (every
one of ``idle_samples`` probes idle); leaving one requires a single busy
sample. Starting work needs evidence the coast is clear, because being wrong
is charged to somebody else's latency. Stopping needs only a hint, because
being wrong costs one sweep interval of delay on a summary nobody is watching.

**Unknown is busy.** A probe that times out, 404s, or returns text without the
gauges yields "not idle". Treating an unreachable vLLM as permission would
make a broken probe indistinguishable from a quiet GPU, in the direction that
spends the resource. Every refusal carries its reason into the log, so "why is
there no digest" stays answerable from the journal -- the rule ``main.py``
already follows for the disabled sweeper.

**What this cannot do.** vLLM has no cross-client priority: once a digest
request is submitted it shares a batch with whatever the loop sends one second
later. The gate stops us from *joining* a busy GPU; it cannot stop a request
already in flight from adding latency to one that arrives behind it. Bounded
by the digest's own duration (~4s measured), and that bound is the whole
argument for this being enough.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

import httpx

from magickit.config import Settings
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

#: The two vLLM gauges that answer "is anybody using the card".
RUNNING_METRIC = "vllm:num_requests_running"
WAITING_METRIC = "vllm:num_requests_waiting"

#: One Prometheus sample line: name, optional label set, value. Anchored at the
#: name so ``# HELP`` / ``# TYPE`` lines cannot match, and the name group stops
#: at ``{`` or whitespace so ``vllm:num_requests_waiting`` is not confused with
#: a hypothetical ``vllm:num_requests_waiting_bucket``.
_SAMPLE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?P<labels>\{[^}]*\})?"
    r"\s+(?P<value>\S+)"
)

Reason = Literal["idle", "busy", "unreachable", "no_metrics", "gate_off"]


@dataclass(frozen=True)
class QueueDepth:
    """How many requests vLLM is serving and how many are queued behind them."""

    running: float
    waiting: float

    def describe(self) -> str:
        return f"running={self.running:g} waiting={self.waiting:g}"


@dataclass(frozen=True)
class Probe:
    """One reading, or the reason there isn't one.

    ``depth is None`` is never "idle": see the module docstring. ``failure``
    distinguishes "could not reach vLLM" from "reached something that is not
    vLLM", because those need different fixes and the log is where that
    distinction has to survive.
    """

    depth: QueueDepth | None = None
    failure: Reason | None = None
    detail: str = ""


@dataclass(frozen=True)
class GateVerdict:
    """The answer, and enough context for the log line to be actionable."""

    idle: bool
    reason: Reason
    observed: str = ""
    samples_taken: int = 0


def parse_queue_depth(metrics_text: str) -> QueueDepth | None:
    """Sum both gauges across every engine and model, or None if unknown.

    Summed rather than read per-model because the question is about the card:
    a second served model would be a second claim on the same GPU, not a
    separate one to be ignored.

    Returns None -- "unknown", which callers must not read as idle -- when
    either gauge is absent, unparseable, or non-finite. NaN matters here
    specifically: ``float('nan') <= 0`` is False, so a NaN would read as busy
    by luck rather than by rule, and ``>=`` in some future threshold would
    flip that to idle. Rejecting it outright is the only stable answer.
    """
    totals: dict[str, float] = {}
    for line in metrics_text.splitlines():
        match = _SAMPLE.match(line.strip())
        if match is None:
            continue
        name = match.group("name")
        if name not in (RUNNING_METRIC, WAITING_METRIC):
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            return None
        if not math.isfinite(value):
            return None
        totals[name] = totals.get(name, 0.0) + value

    if RUNNING_METRIC not in totals or WAITING_METRIC not in totals:
        return None
    return QueueDepth(running=totals[RUNNING_METRIC], waiting=totals[WAITING_METRIC])


@dataclass(frozen=True)
class GpuGateBounds:
    """Every limit, lifted off ``Settings``.

    Separate for the same reason ``DigestBounds`` is: the judging is a pure
    function of these plus a reading, and stays testable without constructing
    a ``Settings``.
    """

    enabled: bool
    metrics_url: str
    idle_samples: int
    sample_interval: float
    probe_timeout: float
    max_running: float
    max_waiting: float

    @classmethod
    def from_settings(cls, settings: Settings) -> GpuGateBounds:
        return cls(
            enabled=settings.digest_sweeper_gpu_idle_only,
            metrics_url=settings.digest_gpu_metrics_url,
            # At least one sample, or "sustained quiet" would be established
            # by asking nothing.
            idle_samples=max(1, settings.digest_gpu_idle_samples),
            sample_interval=max(0.0, settings.digest_gpu_sample_interval_seconds),
            probe_timeout=settings.digest_gpu_probe_timeout_seconds,
            max_running=settings.digest_gpu_max_running,
            max_waiting=settings.digest_gpu_max_waiting,
        )


@dataclass
class GpuIdleGate:
    """Decides whether the sweeper may start, and whether it may continue.

    One instance per process, held on ``app.state.digest_gpu_gate`` beside the
    producer. ``stats`` accumulates a tally per reason for the life of the
    process: the point of shipping this gated rather than simply enabling the
    sweeper is to find out how often the gate actually refuses, and a counter
    in every sweep log line is how that gets answered without new plumbing.
    """

    bounds: GpuGateBounds
    #: Injectable for tests; the default reads vLLM over HTTP.
    probe: Callable[[], Awaitable[Probe]] | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    stats: dict[str, int] = field(default_factory=dict)

    async def confirm_idle(self) -> GateVerdict:
        """Sustained quiet: every sample must be idle, short-circuiting on the first that isn't.

        Costs ``(idle_samples - 1) * sample_interval`` seconds of wall clock in
        the quiet case, spent outside ``run_cycle``'s timeout because it is not
        part of the cycle. A single sample would be measuring the wrong thing:
        the moment right after the loop finishes a turn reads as idle, and is
        the moment it is most likely to send the next one.
        """
        if not self.bounds.enabled:
            return self._tally(GateVerdict(True, "gate_off", "gate disabled"))

        verdict = GateVerdict(True, "idle")
        for index in range(self.bounds.idle_samples):
            if index:
                await self.sleep(self.bounds.sample_interval)
            verdict = self._judge(await self._read(), samples_taken=index + 1)
            if not verdict.idle:
                return self._tally(verdict)
        return self._tally(verdict)

    async def still_idle(self) -> GateVerdict:
        """One sample, no waiting -- the mid-cycle check.

        Deliberately not ``confirm_idle``: pausing 20 seconds between threads
        to re-establish sustained quiet would cost more GPU-adjacent wall clock
        than it saves, and the question here is the cheaper one. We already
        know it was quiet; we only need to notice that it stopped being quiet.
        """
        if not self.bounds.enabled:
            return self._tally(GateVerdict(True, "gate_off", "gate disabled"))
        return self._tally(self._judge(await self._read(), samples_taken=1))

    # --- internals ------------------------------------------------------

    def _judge(self, probe: Probe, *, samples_taken: int) -> GateVerdict:
        if probe.depth is None:
            return GateVerdict(False, probe.failure or "unreachable", probe.detail, samples_taken)
        idle = (
            probe.depth.running <= self.bounds.max_running
            and probe.depth.waiting <= self.bounds.max_waiting
        )
        return GateVerdict(idle, "idle" if idle else "busy", probe.depth.describe(), samples_taken)

    def _tally(self, verdict: GateVerdict) -> GateVerdict:
        self.stats[verdict.reason] = self.stats.get(verdict.reason, 0) + 1
        return verdict

    async def _read(self) -> Probe:
        if self.probe is not None:
            return await self.probe()
        return await self._http_probe()

    async def _http_probe(self) -> Probe:
        """GET the metrics endpoint. A new client per probe, ~8 per sweep interval.

        Cheap enough not to justify a long-lived client that would then need a
        close path on shutdown -- the same trade ``DigestProducer`` makes when
        it builds its adapters per cycle.
        """
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.bounds.probe_timeout)
            ) as client:
                response = await client.get(self.bounds.metrics_url)
                response.raise_for_status()
        except Exception as e:  # noqa: BLE001 - any failure to read is "unknown"
            return Probe(failure="unreachable", detail=f"{type(e).__name__}: {e}")

        depth = parse_queue_depth(response.text)
        if depth is None:
            return Probe(
                failure="no_metrics",
                detail=(
                    f"{RUNNING_METRIC}/{WAITING_METRIC} not readable from {self.bounds.metrics_url}"
                ),
            )
        return Probe(depth=depth)


__all__ = [
    "GateVerdict",
    "GpuGateBounds",
    "GpuIdleGate",
    "Probe",
    "QueueDepth",
    "RUNNING_METRIC",
    "WAITING_METRIC",
    "parse_queue_depth",
]
