"""Produce chatroom thread digests: read Conclair, summarize, PUT back.

Magickit is the producer and Conclair the store. Conclair must stay a leaf
that calls no other Spirrow service (``web/chatroom_proxy.py``'s "No circular
dependency"), and making a digest means calling Cognilens -> Lexora, which is
orchestration. So the LLM work happens here and the result is PUT into
Conclair, which is the one allowed direction.

Lives in ``core/`` because three callers need it: the lifespan sweeper, the
on-demand web route, and (optionally) an MCP tool. Like ``web/ops.py`` it
builds its own adapters from ``Settings`` rather than reaching into
``mcp.tools.chatroom``'s module globals.

Two rules shape the design.

**Freshness comes from Conclair, never from local state.** A digest carries
``source_last_msg_id``, and ``messages`` is append-only, so "covers up to
msg-N" is a permanent fact. Conclair derives ``stale`` from it. That survives
a Magickit restart and stays correct when the on-demand route (or a second
process) writes a digest.

**Failure memory is local, and therefore single-process.** A failed digest
writes *nothing* -- we must never PUT an error as a summary -- so Conclair
cannot remember failures. The backoff table below is in-memory, which means
**only one process may run the sweeper**: ``main.py``'s FastAPI process.
``mcp_server.py`` runs as two systemd units, so registering it there would put
three sweepers on one GPU with three disjoint failure memories.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from magickit.adapters.chatroom import ChatroomAdapter
from magickit.adapters.cognilens import CognilensAdapter, CognilensError
from magickit.config import Settings
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

#: What we ask Lexora for, recorded on every digest. Cognilens resolves the
#: model from its own config (`llm.model: "light"`), so this is the tier we
#: *requested*, not one we observed -- see `_provenance_model`.
REQUESTED_TIER = "light"

SWEEPER_PRODUCER = "magickit-digest-sweeper"
ON_DEMAND_PRODUCER = "magickit-digest-ondemand"

#: Elision marker, deliberately inside the prompt: the model can only say
#: 中略あり if it is told.
_ELISION = "\n\n… 中略 ({omitted} 件 / 約 {chars} 文字を省略) …\n\n"
_MSG_ELISION = "… ({chars} 文字省略) …"

#: Rough ceiling on a digest, as a multiple of the requested token budget.
#: Japanese runs well under 6 chars/token, so anything past this is a runaway
#: decode rather than a long summary.
_RUNAWAY_CHARS_PER_TOKEN = 6


@dataclass(frozen=True)
class DigestBounds:
    """Every limit, lifted off ``Settings``.

    Separate so the two functions that carry most of this module's risk --
    ``build_digest_input`` and ``accept_digest`` -- are pure and testable
    without constructing a ``Settings``. ``web/ops.py::classify`` is the
    precedent: pure, and covered by direct tests with no mocks.
    """

    style: str
    max_tokens: int
    min_msg_count: int
    min_input_chars: int
    max_input_chars: int
    head_chars_ratio: float
    max_msg_chars: int
    min_redigest: timedelta
    max_threads_per_cycle: int
    max_threads_per_project: int
    max_concurrency: int
    include_statuses: tuple[str, ...]
    failure_backoff: timedelta
    failure_backoff_max: timedelta
    max_consecutive_failures: int
    summarize_timeout: float

    @classmethod
    def from_settings(cls, settings: Settings) -> DigestBounds:
        return cls(
            style=settings.digest_style,
            max_tokens=settings.digest_max_tokens,
            min_msg_count=settings.digest_min_msg_count,
            min_input_chars=settings.digest_min_input_chars,
            max_input_chars=settings.digest_max_input_chars,
            head_chars_ratio=settings.digest_head_chars_ratio,
            max_msg_chars=settings.digest_max_msg_chars,
            min_redigest=timedelta(minutes=settings.digest_min_redigest_minutes),
            max_threads_per_cycle=settings.digest_max_threads_per_cycle,
            max_threads_per_project=settings.digest_max_threads_per_project_per_cycle,
            max_concurrency=max(1, settings.digest_max_concurrency),
            include_statuses=tuple(settings.digest_include_statuses),
            failure_backoff=timedelta(minutes=settings.digest_failure_backoff_minutes),
            failure_backoff_max=timedelta(
                minutes=settings.digest_failure_backoff_max_minutes
            ),
            max_consecutive_failures=settings.digest_max_consecutive_failures,
            summarize_timeout=settings.digest_summarize_timeout_seconds,
        )


@dataclass(frozen=True)
class Candidate:
    """A thread the sweeper is considering, from ``list_threads``.

    Everything here comes from the listing's own rollup fields, so cheap
    filtering costs no extra round trip.
    """

    project: str
    thread_id: str
    status: str
    last_msg_id: str | None
    msg_count: int
    last_activity_at: datetime | None


@dataclass(frozen=True)
class DigestInput:
    """The rendered prompt input, plus exactly what it covers."""

    text: str
    source_last_msg_id: str
    #: Messages actually rendered -- not ``thread.msg_count``. A truncated
    #: digest must report what it read, or the record is unauditable.
    source_msg_count: int
    thread_msg_count: int
    truncated: bool
    omitted_msgs: int


@dataclass(frozen=True)
class DigestOutcome:
    """What happened to one thread, in a shape a log line can use."""

    project: str
    thread_id: str
    action: Literal["written", "skipped", "failed"]
    #: A short token, so "why is there no digest" has an answer per thread:
    #: too_short / too_small / fresh / recently_digested / backoff /
    #: read_error / cognilens_error / conclair_error / rejected_* / timeout.
    reason: str
    source_last_msg_id: str | None = None
    input_chars: int = 0
    output_chars: int = 0
    truncated: bool = False
    detail: str = ""


@dataclass
class _FailureRecord:
    count: int
    last_attempt_at: datetime
    #: The thread head when it failed. A new head resets `count`: new
    #: messages are new evidence that it might succeed now, and evidence
    #: that somebody cares. Without the reset, one transient Lexora outage
    #: permanently blacklists whatever was in flight.
    last_msg_id_at_failure: str | None


def _msg_num(msg_id: str | None) -> int:
    """Numeric part of ``msg-NNN``; -1 when unparseable.

    Lexicographic order is wrong (``msg-9 > msg-100``), so any max over
    msg ids goes through this.
    """
    if not msg_id or not msg_id.startswith("msg-"):
        return -1
    try:
        return int(msg_id[4:])
    except ValueError:
        return -1


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _shorten_middle(text: str, limit: int) -> str:
    """Cut the middle out of one message, keeping both ends.

    The middle, not the tail: a pasted log's first lines say what it is and
    its last lines say how it ended, and both are what a summary needs.
    """
    if len(text) <= limit:
        return text
    keep = max(1, (limit - len(_MSG_ELISION)) // 2)
    marker = _MSG_ELISION.format(chars=len(text) - keep * 2)
    return f"{text[:keep]}{marker}{text[-keep:]}"


def _render_message(msg: dict[str, Any], *, max_chars: int) -> str:
    """One message as prompt text.

    Included, and why: ``msg_id`` (so the digest can cite "msg-012 で決定",
    which makes it actionable rather than merely descriptive); ``author``
    (a design thread's meaning is inseparable from who said what);
    ``role``, only when non-null (the role gate guarantees a recorded role
    was validated, so it is trustworthy); ``type`` (handoff / ack / decide
    are the plot); ``timestamp`` truncated to the minute (full ISO is ~500
    tokens of noise over 40 msgs).

    Excluded: ``embodiment`` (operational, not semantic) and the per-msg
    ``tags`` / ``commit_ref`` / ``reply_to`` / ``related_tasks`` (noise at
    this compression ratio).
    """
    msg_id = str(msg.get("msg_id", "?"))
    author = str(msg.get("author", "?"))
    msg_type = str(msg.get("type", "?"))
    role = msg.get("role")
    who = f"{author} ({role})" if role else author
    stamp = _parse_dt(msg.get("timestamp"))
    when = stamp.strftime("%Y-%m-%d %H:%M") if stamp else "?"
    content = _shorten_middle(str(msg.get("content", "")), max_chars)
    return f"## [{msg_id}] {who} — {msg_type} — {when}\n{content}"


def build_digest_input(view: dict[str, Any], bounds: DigestBounds) -> DigestInput:
    """Render a thread into prompt text, elided at message boundaries.

    Oversized threads are cut head+tail rather than compressed by extra LLM
    calls. Three reasons, and they compound:

    1. A thread we cannot fit should be **labelled partial, not silently
       synthesized**. Head+tail keeps the propose (why the thread exists)
       and the latest exchange (where it is stuck) -- which is exactly what
       the dashboard asks for -- and the elision marker goes *into the
       prompt*, so the model can say 中略あり.
    2. ``progressive_compress`` was wire-broken until recently and is
       untested against a live Cognilens; ``compress_context`` is the
       observed-flaky one.
    3. ``unify_summaries`` would cost 4-6 GPU calls where the budget says
       one, and its prompt is built for *multiple documents on the same
       subject* (重複は1度だけ / 矛盾は明記). A chunked thread's chunks are
       sequential, not parallel, so unification's core operation has
       nothing to do and degenerates into lossy concatenation.

    ``source_last_msg_id`` comes from the messages **actually rendered**,
    never from an earlier listing: the listing and the fetch are two round
    trips, and a message landing between them would mark the digest fresh
    for a set it did not summarize.

    Args:
        view: Conclair's ``ThreadView`` (thread + messages).
        bounds: Size limits.

    Returns:
        The rendered input and its coverage.

    Raises:
        ValueError: If the thread has no messages (nothing to cover).
    """
    thread = view.get("thread") or {}
    messages = list(view.get("messages") or [])
    if not messages:
        raise ValueError("thread has no messages")

    header_lines = [f"# スレッド: {thread.get('title', '')}"]
    meta = (
        f"project: {thread.get('project', '')} / thread: {thread.get('thread_id', '')}"
        f" / status: {thread.get('status', '')} / owner: {thread.get('owner', '')}"
    )
    header_lines.append(meta)
    tags = thread.get("tags") or []
    if tags:
        header_lines.append("tags: " + ", ".join(str(t) for t in tags))
    header = "\n".join(header_lines) + "\n\n"

    rendered = [_render_message(m, max_chars=bounds.max_msg_chars) for m in messages]
    body_budget = max(1, bounds.max_input_chars - len(header))
    joined = "\n\n".join(rendered)

    if len(joined) <= body_budget:
        head_count, tail_count, omitted = len(rendered), 0, 0
        body = joined
    else:
        head_budget = int(body_budget * bounds.head_chars_ratio)
        head: list[str] = []
        used = 0
        # Whole messages only. Half a message is half a sentence, and the
        # model will confabulate the rest.
        for block in rendered:
            if used + len(block) > head_budget and head:
                break
            head.append(block)
            used += len(block) + 2
        tail: list[str] = []
        tail_used = 0
        tail_budget = body_budget - used
        for block in reversed(rendered[len(head) :]):
            if tail_used + len(block) > tail_budget and tail:
                break
            tail.insert(0, block)
            tail_used += len(block) + 2
        head_count, tail_count = len(head), len(tail)
        omitted = len(rendered) - head_count - tail_count
        if omitted <= 0:
            # head + tail between them covered every message. Only reachable
            # when a single message is itself larger than the head budget
            # (each loop admits its first block regardless), which needs
            # `max_msg_chars * 2 > max_input_chars` -- a misconfiguration.
            # Still handled rather than trusted: emitting an over-budget body
            # here would spend the ~120s of GPU the ceiling exists to avoid.
            body = joined[:body_budget]
            head_count, tail_count, omitted = len(rendered), 0, 0
        else:
            omitted_chars = sum(
                len(b) for b in rendered[head_count : len(rendered) - tail_count]
            )
            marker = _ELISION.format(omitted=omitted, chars=omitted_chars)
            body = "\n\n".join(head) + marker + "\n\n".join(tail)
        # The separators and the marker are counted approximately above, so
        # clamp once at the end. The ceiling is a timeout, not a preference.
        body = body[:body_budget]

    covered = messages if omitted == 0 else messages[:head_count] + messages[-tail_count:]
    source_last = max(
        (str(m.get("msg_id", "")) for m in covered), key=_msg_num, default=""
    )
    if not source_last:
        raise ValueError("no usable msg_id among the rendered messages")

    return DigestInput(
        text=header + body,
        source_last_msg_id=source_last,
        source_msg_count=len(covered),
        thread_msg_count=int(thread.get("msg_count") or len(messages)),
        truncated=omitted > 0,
        omitted_msgs=max(0, omitted),
    )


def accept_digest(
    summary: str, source: DigestInput, bounds: DigestBounds
) -> tuple[bool, str]:
    """Decide whether a summary is fit to store.

    Belt and braces on top of ``CognilensAdapter``, which now raises rather
    than returning a rejection envelope as prose. The first rule is what
    keeps ``"{'error_type': ...}"`` out of the human UI forever, and it
    stays even though the adapter should make it unreachable.

    Deliberately **not** gated on Cognilens's ``quality_score``: with an
    empty ``preserve`` its preservation term is unconditionally 1.0, so the
    score reduces to a function of output length alone. Rules 3 and 4 do
    that job legibly.

    Returns:
        ``(accepted, reason)``. ``reason`` is empty when accepted.
    """
    stripped = summary.strip()
    if not stripped:
        return False, "rejected_empty"
    if stripped.startswith("{'") or '"error_type"' in stripped[:200]:
        return False, "rejected_error_envelope"
    if len(stripped) >= len(source.text):
        # The model echoed the input. Real with no-think models on short
        # inputs, and a "digest" longer than its source is a lie that
        # pollutes the 全文 / 要約 toggle.
        return False, "rejected_longer_than_source"
    if len(stripped) > bounds.max_tokens * _RUNAWAY_CHARS_PER_TOKEN:
        return False, "rejected_runaway_length"
    return True, ""


def _provenance_model(payload: dict[str, Any]) -> str | None:
    """Which model served the request, when Cognilens says.

    Today it does not: ``tools/summarize.py`` builds its return dict from
    ``CompressionResult`` without ``metadata["model"]``, so this is None and
    the digest records only the tier we *asked for*. Reading the key anyway
    means the field becomes a real observation the day Cognilens returns it,
    with no change here.
    """
    value = payload.get("model")
    return str(value) if isinstance(value, str) and value else None


class DigestProducer:
    """Reads threads from Conclair, summarizes via Cognilens, PUTs back.

    One instance per process, held on ``app.state.digest_producer``. The
    concurrency semaphore lives here rather than on the sweeper so the
    on-demand route shares it: ten button presses must not become ten
    concurrent vLLM requests on the one GPU.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        bounds: DigestBounds | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._bounds = bounds or DigestBounds.from_settings(settings)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._semaphore = asyncio.Semaphore(self._bounds.max_concurrency)
        self._failures: dict[tuple[str, str], _FailureRecord] = {}
        self._project_cursor = 0

    @property
    def bounds(self) -> DigestBounds:
        return self._bounds

    # --- adapters -------------------------------------------------------

    def _chatroom(self) -> ChatroomAdapter:
        return ChatroomAdapter(
            base_url=self._settings.conclair_url,
            timeout=self._settings.conclair_timeout,
        )

    def _cognilens(self) -> CognilensAdapter:
        # Not `settings.cognilens_timeout` (240s): that ceiling is sized for
        # real document work and would outlive the sweep interval, the same
        # mistake ops.PROBE_TIMEOUT documents.
        return CognilensAdapter(
            sse_url=self._settings.cognilens_url,
            timeout=self._bounds.summarize_timeout,
        )

    # --- failure memory -------------------------------------------------

    def _backoff_until(
        self, key: tuple[str, str], last_msg_id: str | None
    ) -> datetime | None:
        record = self._failures.get(key)
        if record is None:
            return None
        if record.last_msg_id_at_failure != last_msg_id:
            # New messages since the failure: new evidence, so start over.
            self._failures.pop(key, None)
            return None
        if record.count >= self._bounds.max_consecutive_failures:
            # Out of the candidate set until the thread moves.
            return datetime.max.replace(tzinfo=timezone.utc)
        delay: timedelta = min(
            self._bounds.failure_backoff * (2 ** (record.count - 1)),
            self._bounds.failure_backoff_max,
        )
        return record.last_attempt_at + delay

    def _record_failure(self, key: tuple[str, str], last_msg_id: str | None) -> None:
        record = self._failures.get(key)
        if record is None or record.last_msg_id_at_failure != last_msg_id:
            self._failures[key] = _FailureRecord(
                count=1, last_attempt_at=self._now(), last_msg_id_at_failure=last_msg_id
            )
            return
        record.count += 1
        record.last_attempt_at = self._now()

    def _clear_failure(self, key: tuple[str, str]) -> None:
        self._failures.pop(key, None)

    # --- candidate selection --------------------------------------------

    async def select_candidates(self, chatroom: ChatroomAdapter) -> list[Candidate]:
        """Pick threads worth probing this cycle.

        Order of calls:

        1. ``list_project_summaries()`` -- one request for every project.
        2. Drop projects with no threads in ``include_statuses``: free.
        3. Rank projects by ``last_activity_at`` desc, then take them
           **round-robin from a persisted cursor**, so a permanently busy
           project cannot starve a quiet one.
        4. ``list_threads`` per project. The returned rows already carry
           ``last_msg_id`` / ``msg_count`` / ``last_activity_at``, so the
           cheap filters apply with no further calls.

        Loop control is deliberately **not** consulted. ``hold`` is a
        statement about the loop taking another turn, not about whether
        anyone may read the project -- and a human sets HOLD precisely when
        they intend to go look at what happened, which is exactly when
        "what is this stuck on" is most wanted. The GPU argument runs the
        same way: a held project's loop is not using the card. "Stop
        everything" is what ``digest.sweeper_enabled`` is for. Reading
        control here would also import its contract that a *failed* read
        means ``hold``, so one Conclair hiccup would silently stop all
        digesting -- a bad failure mode for a cosmetic feature.
        """
        payload = await chatroom.list_project_summaries()
        if "error_type" in payload or "items" not in payload:
            logger.warning(
                "Digest candidate selection: project listing unavailable",
                error=str(payload.get("error", "no items in response")),
            )
            return []

        projects: list[tuple[datetime, str]] = []
        for entry in payload["items"]:
            by_status = entry.get("threads_by_status") or {}
            if not any(by_status.get(s, 0) for s in self._bounds.include_statuses):
                continue
            activity = _parse_dt(entry.get("last_activity_at")) or datetime.min.replace(
                tzinfo=timezone.utc
            )
            projects.append((activity, str(entry.get("project", ""))))
        if not projects:
            return []

        projects.sort(key=lambda pair: pair[0], reverse=True)
        names = [name for _, name in projects if name]
        # Rotate so the cycle does not always start at the busiest project.
        start = self._project_cursor % len(names)
        ordered = names[start:] + names[:start]
        self._project_cursor = (start + 1) % len(names)

        selected: list[Candidate] = []
        for project in ordered:
            if len(selected) >= self._bounds.max_threads_per_cycle:
                break
            project_budget = self._bounds.max_threads_per_project
            listing = await chatroom.list_threads(
                project=project,
                status_filter=list(self._bounds.include_statuses),
                limit=100,
            )
            if "error_type" in listing:
                logger.warning(
                    "Digest candidate selection: thread listing failed",
                    project=project,
                    error=str(listing.get("error", "")),
                )
                continue
            for row in listing.get("items", []):
                if project_budget <= 0:
                    break
                if len(selected) >= self._bounds.max_threads_per_cycle:
                    break
                candidate = Candidate(
                    project=project,
                    thread_id=str(row.get("thread_id", "")),
                    status=str(row.get("status", "")),
                    last_msg_id=row.get("last_msg_id"),
                    msg_count=int(row.get("msg_count") or 0),
                    last_activity_at=_parse_dt(row.get("last_activity_at")),
                )
                if not candidate.thread_id:
                    continue
                if candidate.msg_count < self._bounds.min_msg_count:
                    continue
                selected.append(candidate)
                project_budget -= 1
        return selected

    async def _needs_digest(
        self, chatroom: ChatroomAdapter, candidate: Candidate, *, force: bool
    ) -> tuple[bool, str]:
        """Decide from the stored digest whether this thread is worth a call.

        The freshness answer lives in Conclair, so it survives a restart and
        sees digests written by the on-demand route or another process.
        """
        key = (candidate.project, candidate.thread_id)
        if not force:
            blocked_until = self._backoff_until(key, candidate.last_msg_id)
            if blocked_until is not None and self._now() < blocked_until:
                return False, "backoff"

        stored = await chatroom.get_thread_digest(
            project=candidate.project,
            thread_id=candidate.thread_id,
            style=self._bounds.style,
        )
        if "error_type" in stored:
            # A read failure is not "no digest". Skip rather than spend a
            # call on a guess -- the opposite of the loop-control contract,
            # and cheap either way.
            return False, "read_error"
        if not stored.get("present"):
            return True, ""
        digest = stored.get("digest") or {}
        if not force:
            if not digest.get("stale"):
                return False, "fresh"
            generated_at = _parse_dt(digest.get("generated_at"))
            if (
                generated_at is not None
                and self._now() - generated_at < self._bounds.min_redigest
            ):
                # Distinct from `fresh`: this one *is* out of date, we are
                # just declining to spend the GPU on it yet.
                return False, "recently_digested"
        return True, ""

    # --- the LLM call ---------------------------------------------------

    async def _summarize(
        self, cognilens: CognilensAdapter, text: str, *, style: str
    ) -> tuple[dict[str, Any], int]:
        """One summarize call, bounded twice and serialized by the semaphore.

        ``sse_read_timeout`` bounds a single SSE read, not the whole call,
        so ``asyncio.wait_for`` wraps it as well.
        """
        async with self._semaphore:
            started = time.monotonic()
            payload = await asyncio.wait_for(
                cognilens.summarize_payload(
                    text, style=style, max_tokens=self._bounds.max_tokens
                ),
                timeout=self._bounds.summarize_timeout,
            )
            return payload, int((time.monotonic() - started) * 1000)

    # --- one thread ------------------------------------------------------

    async def digest_thread(
        self,
        *,
        project: str,
        thread_id: str,
        style: str | None = None,
        force: bool = False,
        producer_label: str = ON_DEMAND_PRODUCER,
        chatroom: ChatroomAdapter | None = None,
        cognilens: CognilensAdapter | None = None,
    ) -> DigestOutcome:
        """Digest one thread end to end.

        ``force`` bypasses ``min_redigest`` and the failure backoff -- a
        human pressing the button is new information. It does **not**
        bypass ``min_msg_count`` or the input ceiling: those are about the
        output being worthless and about the GPU, not about staleness, so a
        two-message thread gets an explanatory refusal rather than junk.

        Args:
            project: Project identifier.
            thread_id: Thread to digest.
            style: Override the configured style.
            force: Skip the staleness and backoff gates.
            producer_label: Recorded as ``producer``; the sweeper and the
                button use different values because "why is this 3 hours
                old" has different answers, and someone who pressed the
                button wants to see their press.
            chatroom: Reuse an open adapter (the sweeper does).
            cognilens: Reuse an open adapter (the sweeper does).

        Returns:
            What happened, with a machine-readable ``reason``.
        """
        owns_chatroom = chatroom is None
        room = chatroom or self._chatroom()
        lens = cognilens or self._cognilens()
        effective_style = style or self._bounds.style
        key = (project, thread_id)

        try:
            view = await room.get_thread(
                project=project, thread_id=thread_id, mode="full"
            )
            if "error_type" in view:
                return DigestOutcome(
                    project, thread_id, "failed", "read_error",
                    detail=str(view.get("error", "")),
                )

            thread = view.get("thread") or {}
            msg_count = int(thread.get("msg_count") or 0)
            if msg_count < self._bounds.min_msg_count:
                return DigestOutcome(
                    project, thread_id, "skipped", "too_short",
                    detail=(
                        f"{msg_count} 件のスレッドは、要約より原文の方が"
                        f"短く正確です (下限 {self._bounds.min_msg_count} 件)"
                    ),
                )

            try:
                source = build_digest_input(view, self._bounds)
            except ValueError as e:
                return DigestOutcome(
                    project, thread_id, "skipped", "too_short", detail=str(e)
                )

            if len(source.text) < self._bounds.min_input_chars:
                return DigestOutcome(
                    project, thread_id, "skipped", "too_small",
                    detail=(
                        f"{len(source.text)} 文字は要約するには短すぎます "
                        f"(下限 {self._bounds.min_input_chars} 文字)"
                    ),
                )

            try:
                payload, duration_ms = await self._summarize(
                    lens, source.text, style=effective_style
                )
            except CognilensError as e:
                self._record_failure(key, source.source_last_msg_id)
                logger.warning(
                    "Digest summarize failed",
                    project=project, thread_id=thread_id,
                    error=str(e), error_type=e.error_type,
                )
                return DigestOutcome(
                    project, thread_id, "failed", "cognilens_error",
                    input_chars=len(source.text), detail=str(e),
                )
            except TimeoutError as e:
                self._record_failure(key, source.source_last_msg_id)
                logger.warning(
                    "Digest summarize timed out",
                    project=project, thread_id=thread_id,
                    timeout=self._bounds.summarize_timeout,
                )
                return DigestOutcome(
                    project, thread_id, "failed", "timeout",
                    input_chars=len(source.text), detail=str(e),
                )

            summary = str(payload.get("summary", ""))
            accepted, reject_reason = accept_digest(summary, source, self._bounds)
            if not accepted:
                # A thread whose content reliably produces an echo should
                # not be retried every cycle, so this counts as a failure.
                self._record_failure(key, source.source_last_msg_id)
                logger.warning(
                    "Digest rejected before storing",
                    project=project, thread_id=thread_id, reason=reject_reason,
                    input_chars=len(source.text), output_chars=len(summary),
                )
                return DigestOutcome(
                    project, thread_id, "failed", reject_reason,
                    input_chars=len(source.text), output_chars=len(summary),
                )

            stored = await room.put_thread_digest(
                project=project,
                thread_id=thread_id,
                digest=summary.strip(),
                source_last_msg_id=source.source_last_msg_id,
                source_msg_count=source.source_msg_count,
                producer=producer_label,
                style=effective_style,
                truncated=source.truncated,
                model=_provenance_model(payload),
                # What we asked Lexora for. Which model actually served is a
                # deploy fact of spirrow-cognilens/config.yaml that Magickit
                # does not read -- see _provenance_model.
                tier=REQUESTED_TIER,
                source_chars=len(source.text),
                input_tokens=payload.get("original_tokens"),
                output_tokens=payload.get("compressed_tokens"),
                duration_ms=duration_ms,
            )
            if "error_type" in stored:
                self._record_failure(key, source.source_last_msg_id)
                logger.warning(
                    "Digest store failed",
                    project=project, thread_id=thread_id,
                    error=str(stored.get("error", "")),
                )
                return DigestOutcome(
                    project, thread_id, "failed", "conclair_error",
                    input_chars=len(source.text), output_chars=len(summary),
                    detail=str(stored.get("error", "")),
                )

            self._clear_failure(key)
            logger.info(
                "digest written",
                project=project,
                thread_id=thread_id,
                producer=producer_label,
                style=effective_style,
                source_last_msg_id=source.source_last_msg_id,
                source_msg_count=source.source_msg_count,
                thread_msg_count=source.thread_msg_count,
                truncated=source.truncated,
                omitted_msgs=source.omitted_msgs,
                input_chars=len(source.text),
                output_chars=len(summary),
                original_tokens=payload.get("original_tokens"),
                compressed_tokens=payload.get("compressed_tokens"),
                # Recorded, never gated on: with an empty `preserve` this is
                # a function of output length alone.
                quality_score=payload.get("quality_score"),
                duration_ms=duration_ms,
            )
            return DigestOutcome(
                project, thread_id, "written", "ok",
                source_last_msg_id=source.source_last_msg_id,
                input_chars=len(source.text),
                output_chars=len(summary),
                truncated=source.truncated,
            )
        finally:
            # Only BaseAdapter subclasses have a real close(). Calling it on
            # the MCP adapter would fire a bogus `close` tool over the wire,
            # because __getattr__ fabricates any unknown attribute.
            if owns_chatroom:
                await room.close()

    # --- one cycle -------------------------------------------------------

    async def run_cycle(self) -> list[DigestOutcome]:
        """Select, filter, and digest up to the per-cycle budget.

        Sequential by construction (``max_concurrency`` defaults to 1):
        there is one GPU, so parallelism buys no throughput and only makes
        the coding loop's own requests queue behind these.
        """
        chatroom = self._chatroom()
        cognilens = self._cognilens()
        outcomes: list[DigestOutcome] = []
        try:
            candidates = await self.select_candidates(chatroom)
            for candidate in candidates:
                needed, skip_reason = await self._needs_digest(
                    chatroom, candidate, force=False
                )
                if not needed:
                    outcomes.append(
                        DigestOutcome(
                            candidate.project, candidate.thread_id,
                            "skipped", skip_reason,
                        )
                    )
                    continue
                outcomes.append(
                    await self.digest_thread(
                        project=candidate.project,
                        thread_id=candidate.thread_id,
                        producer_label=SWEEPER_PRODUCER,
                        chatroom=chatroom,
                        cognilens=cognilens,
                    )
                )
        finally:
            await chatroom.close()
        return outcomes


async def sweep_forever(
    producer: DigestProducer, *, interval: float, cycle_timeout: float
) -> None:
    """Run one digest cycle per interval, forever.

    **Sleeps first, not last.** Startup is when this process is busiest
    (migrations, the first dashboard poll) and when the coding loop is most
    likely mid-turn on the GPU. It also means a cycle that crashes
    immediately cannot hot-loop -- the sleep at the top is the backoff.

    Args:
        producer: The shared producer (its semaphore bounds GPU use).
        interval: Seconds between cycles.
        cycle_timeout: Hard stop for one cycle. A cycle that outlives its
            own interval is the ops.PROBE_TIMEOUT lesson applied here.
    """
    while True:
        try:
            await asyncio.sleep(interval)
            outcomes = await asyncio.wait_for(
                producer.run_cycle(), timeout=cycle_timeout
            )
            logger.info(
                "digest sweep complete",
                written=sum(1 for o in outcomes if o.action == "written"),
                skipped=sum(1 for o in outcomes if o.action == "skipped"),
                failed=sum(1 for o in outcomes if o.action == "failed"),
                reasons=_reason_counts(outcomes),
            )
        except asyncio.CancelledError:
            # Must re-raise, or the sweeper cannot be cancelled and shutdown
            # hangs on it.
            raise
        except Exception as e:  # noqa: BLE001 - a bad cycle must not end the loop
            logger.warning("digest sweep cycle failed", error=str(e))


def _reason_counts(outcomes: Sequence[DigestOutcome]) -> dict[str, int]:
    """Per-reason tally, so "why is there no digest" is answerable from a log."""
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.reason] = counts.get(outcome.reason, 0) + 1
    return counts


__all__ = [
    "Candidate",
    "DigestBounds",
    "DigestInput",
    "DigestOutcome",
    "DigestProducer",
    "ON_DEMAND_PRODUCER",
    "REQUESTED_TIER",
    "SWEEPER_PRODUCER",
    "accept_digest",
    "build_digest_input",
    "sweep_forever",
]
