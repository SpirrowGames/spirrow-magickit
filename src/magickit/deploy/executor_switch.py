"""Executor switch reports into deploy_history (ADR-2026-10-09-23 D12).

D12 (text pinned at spirrow-mindwire e942f56): the executor (mindwire) is
the recorder of switch times and reports "which machine switched when" to
Magickit's deploy_history. The executor's current version lives in the
dynamic report, which is overwritten every tick and keeps no history;
**the switch history is held by deploy_history alone.** Nothing else can
rebuild it, so a switch record is only ever appended: there is no API that
edits or deletes one.

Checks, in this order. Every refusal writes nothing.

1. **Executor name -- form only.** Magickit does not look the name up
   anywhere: the ledger is the Conductor's ``[executors.<name>]`` config and
   only the Conductor reads it (D6, §2.2). The name must be usable as a
   bare TOML key there (``[A-Za-z0-9_-]+``), else ``invalid_executor``.
   A mistyped name is caught on the Conductor's side, by §7(f)'s ledger vs.
   dynamic-report comparison: the executor takes its name from one setting
   for both reports, so a typo shows up there as "in the ledger, no report".
2. **to_tag.** Exactly ``executor/vX.Y.Z`` (``invalid_tag``) and present in
   the tag repository as an annotated tag (``tag_not_found`` /
   ``tag_not_annotated`` / ``github_error``).
3. **The previous record and ``first_switch``** -- four cases, no others:

   ==============  ==============  ======================================
   previous record  first_switch    result
   ==============  ==============  ======================================
   exists           False           accepted; from_tag = previous to_tag
   none             True            accepted; from_tag = null
   none             False           ``no_prior_switch``
   exists           True            ``already_bootstrapped``
   ==============  ==============  ======================================

   ``from_tag`` is derived, never taken from the caller, and is null only
   on an accepted first switch. Whether a switch is the first is decided
   from deploy_history's own records, not from any other store.

Reading the previous record and appending the new one happen under the
deploy store's lock (:meth:`DeployStore.target_lock`), keyed per executor.
That lock is non-blocking: a report racing another report for the same
executor is refused with ``switch_in_progress`` and writes nothing; the
reporter retries.

``switched_at`` is the reporter's own claim and is kept as given; the
record's ``at`` is when Magickit accepted it. The two are never merged.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from magickit.deploy import executor_tags, records
from magickit.deploy.executor_tags import ExecutorTagError, TagApi

#: A bare TOML key: what ``[executors.<name>]`` accepts without quoting.
_EXECUTOR_NAME = re.compile(r"[A-Za-z0-9_-]+")

#: Lock key prefix. ``@`` cannot appear in a deploy target name, so this
#: never shares a lock file with a deploy.
_LOCK_PREFIX = "executor-switch@"


def validate_executor_name(executor: str) -> str:
    if not isinstance(executor, str) or not _EXECUTOR_NAME.fullmatch(executor):
        raise ExecutorTagError(
            "invalid_executor",
            "executor must be usable as the ledger key [executors.<name>] "
            f"(letters, digits, '_' and '-'); got {executor!r}",
        )
    return executor


def _validate_switched_at(switched_at: str) -> str:
    try:
        parsed = datetime.fromisoformat(switched_at) if isinstance(switched_at, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise ExecutorTagError(
            "invalid_argument",
            f"switched_at must be ISO 8601 with a UTC offset; got {switched_at!r}",
        )
    return switched_at


def _require_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutorTagError("invalid_argument", f"{name} must be a non-empty string")
    return value.strip()


async def report_executor_switch(
    api: TagApi,
    store: records.DeployStore,
    *,
    repo: str,
    executor: str,
    to_tag: str,
    switched_at: str,
    reported_by: str,
    reason: str,
    reservation_id: str = "",
    first_switch: bool = False,
) -> dict[str, Any]:
    """Validate a switch report and append it. See the module docstring.

    Raises:
        ExecutorTagError: on any refusal; nothing has been written.
    """
    # 1. the name (form only), then everything else that needs no network.
    executor = validate_executor_name(executor)
    to_tag = executor_tags.parse_executor_tag(to_tag)
    switched_at = _validate_switched_at(switched_at)
    reported_by = _require_text("reported_by", reported_by)
    reason = _require_text("reason", reason)
    if not isinstance(reservation_id, str):
        raise ExecutorTagError("invalid_argument", "reservation_id must be a string")
    if not isinstance(first_switch, bool):
        raise ExecutorTagError("invalid_argument", "first_switch must be true or false")

    # 2. the tag exists, annotated.
    await executor_tags.verify_annotated_tag(api, repo=repo, tag=to_tag)

    # 3. the four-case table, read and append under one lock.
    try:
        with store.target_lock(f"{_LOCK_PREFIX}{executor}"):
            previous = store.latest_executor_switch(executor)
            if previous is None and not first_switch:
                raise ExecutorTagError(
                    "no_prior_switch",
                    f"no switch has been recorded for {executor}. If this is its "
                    "first switch, report again with first_switch=true.",
                )
            if previous is not None and first_switch:
                raise ExecutorTagError(
                    "already_bootstrapped",
                    f"{executor} already has switch history (last to_tag "
                    f"{previous.get('to_tag')!r} at {previous.get('at')!r}); "
                    "report without first_switch.",
                )
            from_tag = None if previous is None else previous.get("to_tag")
            received_at = records.utcnow()
            store.audit(
                records.EVENT_EXECUTOR_SWITCH,
                at=received_at,  # when Magickit accepted it; not switched_at
                executor=executor,
                from_tag=from_tag,
                to_tag=to_tag,
                first_switch=first_switch,
                switched_at=switched_at,
                reported_by=reported_by,
                # `actor` is the field every other audit event names its
                # caller with; the deploys page reads it.
                actor=reported_by,
                reason=reason,
                reservation_id=reservation_id or None,
            )
    except records.DeployLockedError as exc:
        raise ExecutorTagError(
            "switch_in_progress",
            f"another switch report for {executor} is being recorded; retry ({exc})",
        ) from exc

    return {
        "ok": True,
        "executor": executor,
        "from_tag": from_tag,
        "to_tag": to_tag,
        "first_switch": first_switch,
        "switched_at": switched_at,
        "received_at": received_at,
    }
