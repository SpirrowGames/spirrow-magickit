"""Unit tests for the sweeper's GPU idle gate.

Like ``test_digest_producer.py``, most of the risk is in a pure function --
``parse_queue_depth`` -- so most of these tests take no mocks. The gate itself
takes an injected probe rather than an HTTP mock, because what is worth
pinning is the *policy* (how many samples, which way an unknown reading
falls), not httpx.

The single most important test here is
``test_an_unreachable_probe_is_busy_not_idle``: fail-open would make a broken
metrics URL indistinguishable from a quiet GPU, in the direction that spends
somebody else's latency.
"""

from __future__ import annotations

from typing import Any

from magickit.core.gpu_gate import (
    GpuGateBounds,
    GpuIdleGate,
    Probe,
    QueueDepth,
    parse_queue_depth,
)

#: Trimmed from the live endpoint on 2026-08-28, labels and all.
LIVE_SAMPLE = """\
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="Qwen3.8-27B"} 0.0
# HELP vllm:num_requests_waiting Prometheus metric for the number of queued requests.
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{engine="0",model_name="Qwen3.8-27B"} 0.0
# HELP vllm:num_preemptions_total Cumulative number of preemptions from the engine.
# TYPE vllm:num_preemptions_total counter
vllm:num_preemptions_total{engine="0",model_name="Qwen3.8-27B"} 0.0
vllm:num_preemptions_created{engine="0",model_name="Qwen3.8-27B"} 1.7877816479156535e+09
"""


def _bounds(**overrides: Any) -> GpuGateBounds:
    base: dict[str, Any] = {
        "enabled": True,
        "metrics_url": "http://localhost:8000/metrics",
        "idle_samples": 3,
        "sample_interval": 10.0,
        "probe_timeout": 5.0,
        "max_running": 0,
        "max_waiting": 0,
    }
    base.update(overrides)
    return GpuGateBounds(**base)


def _gate(*probes: Probe, **overrides: Any) -> tuple[GpuIdleGate, dict[str, list[Any]]]:
    """A gate over a fixed probe script, plus a record of what it did.

    The last probe repeats, so a test that only cares about "all idle" does
    not have to count how many samples the bounds ask for.
    """
    log: dict[str, list[Any]] = {"probes": [], "sleeps": []}
    queue = list(probes)

    async def _probe() -> Probe:
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        log["probes"].append(result)
        return result

    async def _sleep(seconds: float) -> None:
        log["sleeps"].append(seconds)

    return GpuIdleGate(_bounds(**overrides), probe=_probe, sleep=_sleep), log


def _idle() -> Probe:
    return Probe(depth=QueueDepth(running=0.0, waiting=0.0))


def _busy(running: float = 1.0, waiting: float = 0.0) -> Probe:
    return Probe(depth=QueueDepth(running=running, waiting=waiting))


# =====================================================================
# parse_queue_depth
# =====================================================================


def test_the_live_endpoint_shape_parses() -> None:
    assert parse_queue_depth(LIVE_SAMPLE) == QueueDepth(running=0.0, waiting=0.0)


def test_both_gauges_are_summed_across_engines_and_models() -> None:
    """The question is about the card, so a second model is a second claim."""
    text = (
        'vllm:num_requests_running{engine="0",model_name="a"} 2.0\n'
        'vllm:num_requests_running{engine="1",model_name="b"} 3.0\n'
        'vllm:num_requests_waiting{engine="0",model_name="a"} 1.0\n'
        'vllm:num_requests_waiting{engine="1",model_name="b"} 4.0\n'
    )
    assert parse_queue_depth(text) == QueueDepth(running=5.0, waiting=5.0)


def test_a_missing_gauge_is_unknown_rather_than_zero() -> None:
    """Absence must not read as "nothing running"; that is fail-open."""
    text = 'vllm:num_requests_running{engine="0"} 0.0\n'
    assert parse_queue_depth(text) is None


def test_an_empty_body_is_unknown() -> None:
    assert parse_queue_depth("") is None


def test_a_nan_value_is_unknown() -> None:
    """`nan <= 0` is False, so a NaN would read as busy by luck, not by rule."""
    text = "vllm:num_requests_running NaN\nvllm:num_requests_waiting 0.0\n"
    assert parse_queue_depth(text) is None


def test_an_infinite_value_is_unknown() -> None:
    text = "vllm:num_requests_running +Inf\nvllm:num_requests_waiting 0.0\n"
    assert parse_queue_depth(text) is None


def test_a_longer_metric_name_with_the_same_prefix_is_not_mistaken_for_the_gauge() -> None:
    """`..._waiting_bucket` must not be summed into `..._waiting`."""
    text = 'vllm:num_requests_running 0.0\nvllm:num_requests_waiting_bucket{le="1"} 900.0\n'
    assert parse_queue_depth(text) is None


def test_unlabelled_samples_parse() -> None:
    """Labels are optional in the exposition format."""
    text = "vllm:num_requests_running 1\nvllm:num_requests_waiting 2\n"
    assert parse_queue_depth(text) == QueueDepth(running=1.0, waiting=2.0)


def test_an_html_error_page_is_unknown_rather_than_a_crash() -> None:
    assert parse_queue_depth("<html><body>404 Not Found</body></html>") is None


# =====================================================================
# confirm_idle -- sustained quiet
# =====================================================================


async def test_every_sample_must_be_idle_before_a_cycle_starts() -> None:
    gate, log = _gate(_idle())

    verdict = await gate.confirm_idle()

    assert verdict.idle is True
    assert verdict.reason == "idle"
    assert len(log["probes"]) == 3
    # Two waits between three samples, not three.
    assert log["sleeps"] == [10.0, 10.0]


async def test_one_sample_is_not_enough_to_call_the_gpu_idle() -> None:
    """The moment after the loop finishes a turn reads idle and is exactly
    when the next request is coming."""
    gate, log = _gate(_idle(), _busy(), _idle())

    verdict = await gate.confirm_idle()

    assert verdict.idle is False
    assert verdict.reason == "busy"
    assert verdict.samples_taken == 2


async def test_a_busy_first_sample_short_circuits() -> None:
    """No reason to keep sampling, and no reason to wait 20s to say no."""
    gate, log = _gate(_busy())

    verdict = await gate.confirm_idle()

    assert verdict.idle is False
    assert len(log["probes"]) == 1
    assert log["sleeps"] == []


async def test_an_unreachable_probe_is_busy_not_idle() -> None:
    """Fail-open would make a broken URL look like a quiet GPU."""
    gate, _ = _gate(Probe(failure="unreachable", detail="ConnectError: refused"))

    verdict = await gate.confirm_idle()

    assert verdict.idle is False
    assert verdict.reason == "unreachable"
    assert "ConnectError" in verdict.observed


async def test_a_body_without_the_gauges_is_busy_not_idle() -> None:
    gate, _ = _gate(Probe(failure="no_metrics", detail="not readable"))

    verdict = await gate.confirm_idle()

    assert verdict.idle is False
    assert verdict.reason == "no_metrics"


async def test_queued_requests_alone_make_the_gpu_busy() -> None:
    """Nothing running but three queued still means somebody is waiting."""
    gate, _ = _gate(_busy(running=0.0, waiting=3.0))

    assert (await gate.confirm_idle()).idle is False


async def test_the_running_threshold_is_configurable() -> None:
    """`max_running: 1` is a real position to take once the skip rate is known."""
    gate, _ = _gate(_busy(running=1.0), max_running=1)

    assert (await gate.confirm_idle()).idle is True


async def test_a_disabled_gate_answers_idle_without_probing() -> None:
    """One code path through the sweeper, not two."""
    gate, log = _gate(_busy(), enabled=False)

    verdict = await gate.confirm_idle()

    assert verdict.idle is True
    assert verdict.reason == "gate_off"
    assert log["probes"] == []


async def test_a_single_sample_configuration_takes_no_wait() -> None:
    gate, log = _gate(_idle(), idle_samples=1)

    assert (await gate.confirm_idle()).idle is True
    assert log["sleeps"] == []


# =====================================================================
# still_idle -- the mid-cycle check
# =====================================================================


async def test_the_mid_cycle_check_takes_one_sample_and_does_not_wait() -> None:
    """Asymmetric on purpose: entering needs proof, leaving needs a hint."""
    gate, log = _gate(_idle())

    verdict = await gate.still_idle()

    assert verdict.idle is True
    assert len(log["probes"]) == 1
    assert log["sleeps"] == []


async def test_the_mid_cycle_check_yields_on_a_single_busy_sample() -> None:
    gate, _ = _gate(_busy())

    assert (await gate.still_idle()).idle is False


async def test_a_disabled_gate_never_stops_a_cycle_midway() -> None:
    gate, log = _gate(_busy(), enabled=False)

    assert (await gate.still_idle()).idle is True
    assert log["probes"] == []


# =====================================================================
# stats -- the evidence for eventually removing the gate
# =====================================================================


async def test_every_verdict_is_tallied_by_reason() -> None:
    """The observation period needs no plumbing beyond this counter."""
    gate, _ = _gate(_busy())
    await gate.confirm_idle()
    await gate.still_idle()

    assert gate.stats == {"busy": 2}


async def test_the_tally_survives_across_mixed_verdicts() -> None:
    gate, _ = _gate(_idle())
    await gate.confirm_idle()
    await gate.still_idle()

    assert gate.stats == {"idle": 2}


# =====================================================================
# bounds
# =====================================================================


def test_zero_samples_is_read_as_one_rather_than_as_no_gate() -> None:
    """`idle_samples: 0` would otherwise establish sustained quiet by asking
    nothing, which is the opposite of what the field means."""

    class _S:
        digest_sweeper_gpu_idle_only = True
        digest_gpu_metrics_url = "http://localhost:8000/metrics"
        digest_gpu_idle_samples = 0
        digest_gpu_sample_interval_seconds = 10.0
        digest_gpu_probe_timeout_seconds = 5.0
        digest_gpu_max_running = 0
        digest_gpu_max_waiting = 0

    bounds = GpuGateBounds.from_settings(_S())  # type: ignore[arg-type]

    assert bounds.idle_samples == 1
