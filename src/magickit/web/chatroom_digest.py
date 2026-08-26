"""On-demand digest generation for one thread.

``POST /ui/projects/{project}/threads/{thread_id}/digest`` — the sibling of
``/messages``, which ``chatroom_writes`` already claims. **Registered before
``chatroom_proxy``** for the same reason: Magickit is the producer (Cognilens
and the GPU are on this side), so forwarding it to a Conclair that has no such
route -- and by the leaf constraint cannot have one -- would 404.

Consequence, stated plainly: the button works through Magickit (:8443) and not
through a direct Conclair tunnel (:8115). The proxy injects
``X-Spirrow-Via: magickit`` so Conclair can render the button only where it
works; :8115 is a tunnel, not the intended path, which is the position
``chatroom_proxy.chatroom_loop_control``'s docstring already takes.

**Why shipping this at all, when the sweeper exists.** The sweeper's default is
off, because it is the only thing in Magickit that would start using the local
GPU with nobody asking. A feature whose only surface is a background job that
is off by default has no way to be tried, tuned, or judged. The button is how
it gets evaluated; the sweeper is how it gets cheap once it has earned trust.
Both ship, defaulted oppositely.

**Synchronous, with a hard bound.** The person pressed a button and wants the
digest, not a promise; HTMX's ``hx-indicator`` gives a free spinner; and
fire-and-forget would need a polling endpoint and a job table for a ~20-second
operation. But it is bounded (``digest.on_demand_timeout_seconds``) and it
takes the producer's semaphore, so ten presses do not become ten concurrent
vLLM requests on the one GPU.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Form, Request, Response

from magickit.config import get_settings
from magickit.core.digest_producer import ON_DEMAND_PRODUCER, DigestProducer
from magickit.utils.logging import get_logger
from magickit.web.chatroom_writes import _error_flash, _flash

logger = get_logger(__name__)

router = APIRouter(tags=["chatroom-ui"])

#: Distinct from `messagePosted`: a digest is not a post, and Conclair's page
#: may bind the two differently.
DIGEST_TRIGGER = "digestGenerated"

#: Skip reasons that are a refusal to act rather than a failure, plus the
#: sentence to show. Rendered as an error-styled flash even though nothing
#: broke, because `conclair.js` auto-dismisses `.alert-success` after 6
#: seconds and a refusal the reader misses is a refusal they will retry.
_REFUSALS = {
    "too_short": "要約しませんでした",
    "too_small": "要約しませんでした",
}


def _producer(request: Request) -> DigestProducer | None:
    """The process-wide producer, or None if this app never built one.

    Built unconditionally in ``main.py``'s lifespan, so ``None`` here means
    an app assembled without that lifespan (a test client, or a future
    entry point) rather than a disabled feature.
    """
    producer = getattr(request.app.state, "digest_producer", None)
    return producer if isinstance(producer, DigestProducer) else None


@router.post("/ui/projects/{project}/threads/{thread_id}/digest")
async def generate_digest(
    request: Request,
    project: str,
    thread_id: str,
    style: Annotated[str, Form()] = "",
) -> Response:
    """Generate this thread's digest now and store it in Conclair."""
    settings = get_settings()

    if not settings.digest_on_demand_enabled:
        # A flash, not a 404. A button that renders and then 404s is exactly
        # the loop-control 405 trap CLAUDE.md records: the failure looks like
        # a bug in the page rather than a setting.
        return _error_flash(
            {
                "error_type": "DigestDisabled",
                "error": "要約生成は無効化されています (digest.on_demand_enabled)",
            }
        )

    producer = _producer(request)
    if producer is None:
        return _error_flash(
            {
                "error_type": "DigestUnavailable",
                "error": "要約 producer が初期化されていません",
            }
        )

    try:
        # `force=True`: a human pressing the button is new information, so
        # skip min_redigest and the failure backoff. It does NOT skip
        # min_msg_count or the input ceiling -- those are about the output
        # being worthless and about the GPU, not about staleness.
        outcome = await asyncio.wait_for(
            producer.digest_thread(
                project=project,
                thread_id=thread_id,
                style=style or None,
                force=True,
                producer_label=ON_DEMAND_PRODUCER,
            ),
            timeout=settings.digest_on_demand_timeout_seconds,
        )
    except TimeoutError:
        logger.warning(
            "On-demand digest timed out",
            project=project,
            thread_id=thread_id,
            timeout=settings.digest_on_demand_timeout_seconds,
        )
        return _error_flash(
            {
                "error_type": "DigestTimeout",
                "error": (
                    f"{int(settings.digest_on_demand_timeout_seconds)} 秒以内に"
                    "要約が返りませんでした。GPU が混んでいる可能性があります"
                ),
            }
        )
    except Exception as e:  # noqa: BLE001 - a dead panel must not 500 the page
        logger.error(
            "On-demand digest failed",
            project=project,
            thread_id=thread_id,
            error=str(e),
        )
        return _error_flash({"error_type": type(e).__name__, "error": str(e)})

    if outcome.action == "written":
        return _flash(
            f"要約を生成しました ({outcome.source_last_msg_id} まで"
            f"{'、中略あり' if outcome.truncated else ''})",
            trigger=DIGEST_TRIGGER,
        )

    if outcome.reason in _REFUSALS:
        # An explanation, not a complaint: the refusal teaches the rule
        # ("a 2-message thread's original is shorter and more accurate").
        return _error_flash(
            {"error_type": _REFUSALS[outcome.reason], "error": outcome.detail}
        )

    return _error_flash(
        {
            "error_type": f"DigestFailed:{outcome.reason}",
            "error": outcome.detail or "要約を生成できませんでした",
        }
    )
