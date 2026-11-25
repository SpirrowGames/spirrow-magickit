"""Rate limits for the unauthenticated probe door.

``host_probe`` is registered on the no-auth tailnet instance, so how often
it can be called is bounded here rather than by who calls. The numbers come
from measuring the host (2026-11-25, msg-1757 M7): the heaviest probe,
``journal_summary`` over the busiest target (~30k entries in 52h), takes on
the order of a second; the limits keep a runaway caller to a small fraction
of one core and of the journal's read bandwidth.

State is per process. The two MCP instances count separately, which is fine:
the bound that matters is the unauthenticated one, and it is one process.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Window:
    """At most ``limit`` calls per ``seconds``."""

    limit: int
    seconds: float


#: Every probe, together.
GLOBAL_WINDOWS = (Window(30, 60.0), Window(300, 3600.0))
#: Per probe, on top of the global windows. Probes not listed share only
#: the global ones.
PROBE_WINDOWS: dict[str, tuple[Window, ...]] = {
    "journal_summary": (Window(6, 60.0), Window(60, 3600.0)),
    "health": (Window(12, 60.0),),
}
#: Probes running at once. journalctl and the health wait are the slow
#: ones; two at a time keeps the process responsive.
MAX_CONCURRENT = 2


class RateLimiter:
    """Sliding-window counters, keyed by probe name."""

    def __init__(
        self,
        *,
        global_windows: tuple[Window, ...] = GLOBAL_WINDOWS,
        probe_windows: dict[str, tuple[Window, ...]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._global = global_windows
        self._per_probe = PROBE_WINDOWS if probe_windows is None else probe_windows
        self._clock = clock
        self._calls: dict[str, deque[float]] = {}
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    def _retry_after(self, key: str, windows: tuple[Window, ...], now: float) -> float:
        calls = self._calls.setdefault(key, deque())
        longest = max((w.seconds for w in windows), default=0.0)
        while calls and now - calls[0] > longest:
            calls.popleft()
        wait = 0.0
        for w in windows:
            in_window = [t for t in calls if now - t <= w.seconds]
            if len(in_window) >= w.limit:
                wait = max(wait, w.seconds - (now - in_window[-w.limit]))
        return wait

    def admit(self, probe: str) -> float:
        """Record a call to ``probe`` if allowed; else return seconds to wait.

        Returns:
            0.0 when the call is admitted (and counted), otherwise the
            number of seconds after which it would be.
        """
        now = self._clock()
        windows = self._per_probe.get(probe, ())
        wait = max(
            self._retry_after("*", self._global, now),
            self._retry_after(probe, windows, now) if windows else 0.0,
        )
        if wait > 0:
            return wait
        self._calls.setdefault("*", deque()).append(now)
        if windows:
            self._calls.setdefault(probe, deque()).append(now)
        return 0.0
