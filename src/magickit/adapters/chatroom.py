"""Adapter for spirrow-conclair chatroom backend."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from magickit.adapters.base import BaseAdapter
from magickit.utils.logging import get_logger

logger = get_logger(__name__)


class ChatroomAdapter(BaseAdapter):
    """HTTP adapter for spirrow-conclair (port 8115).

    Thin wrapper that delegates each chatroom operation to the
    corresponding REST endpoint and returns the response JSON as a dict.
    Conclair's error envelope (`{error_type, error, details?}`) is
    forwarded unchanged when status >= 400; callers / MCP tools surface
    it to the user.
    """

    async def health_check(self) -> bool:
        try:
            response = await self._get("/health")
            return response.status_code == 200
        except httpx.HTTPError as e:
            logger.warning("Conclair health check failed", error=str(e))
            return False

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _isoformat(value: datetime | str | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if value.tzinfo is None:
            return value.isoformat() + "Z"
        return value.isoformat()

    async def _request_json(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            logger.error("Conclair request failed", method=method, path=path, error=str(e))
            raise
        if response.status_code >= 400:
            # Pass conclair's structured error envelope through. MCP tools
            # / callers can branch on `error_type` to surface the right
            # affordance to users.
            try:
                return response.json()
            except ValueError:
                return {
                    "error_type": "ConclairUpstreamError",
                    "error": f"non-JSON {response.status_code} response",
                    "details": {"text": response.text[:500]},
                }
        return response.json()

    # --- write endpoints ------------------------------------------------

    async def open_thread(
        self,
        *,
        project: str,
        thread_id: str,
        title: str,
        owner: str,
        propose_content: str,
        tags: list[str] | None = None,
        commit_ref: str | None = None,
        timestamp: datetime | str | None = None,
        embodiment: str | None = None,
        role: str | None = None,
        next_participant: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "thread_id": thread_id,
            "title": title,
            "owner": owner,
            "propose_content": propose_content,
        }
        if tags is not None:
            body["tags"] = tags
        if commit_ref is not None:
            body["commit_ref"] = commit_ref
        if timestamp is not None:
            body["timestamp"] = self._isoformat(timestamp)
        if embodiment is not None:
            body["embodiment"] = embodiment
        if role is not None:
            body["role"] = role
        # T-handoff-target-structured-field msg-068 §4-1. Forward the
        # structured handoff target when the caller supplied one; the meaning
        # of the value (registered identity vs. reserved word) is decided at
        # the enforcement layer above, this adapter is a thin wrapper.
        # Passthrough is unconditional-on-non-None so a stale client that
        # never learned about the field cannot force the receipt into "field
        # accepted but never actually sent" (msg-068 §5 the role trap).
        if next_participant is not None:
            body["next_participant"] = next_participant
        return await self._request_json(
            "POST", f"/v1/projects/{project}/threads", json=body
        )

    async def post_message(
        self,
        *,
        project: str,
        thread_id: str,
        type: str,
        author: str,
        content: str,
        reply_to: str | None = None,
        references_threads: list[str] | None = None,
        related_tasks: list[str] | None = None,
        closes_thread: str | None = None,
        tags: list[str] | None = None,
        commit_ref: str | None = None,
        timestamp: datetime | str | None = None,
        embodiment: str | None = None,
        role: str | None = None,
        owner_override: bool = False,
        owner_override_reason: str | None = None,
        next_participant: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": type,
            "author": author,
            "content": content,
        }
        if owner_override:
            body["owner_override"] = True
        if owner_override_reason is not None:
            body["owner_override_reason"] = owner_override_reason
        if reply_to is not None:
            body["reply_to"] = reply_to
        if references_threads is not None:
            body["references_threads"] = references_threads
        if related_tasks is not None:
            body["related_tasks"] = related_tasks
        if closes_thread is not None:
            body["closes_thread"] = closes_thread
        if tags is not None:
            body["tags"] = tags
        if commit_ref is not None:
            body["commit_ref"] = commit_ref
        if timestamp is not None:
            body["timestamp"] = self._isoformat(timestamp)
        if embodiment is not None:
            body["embodiment"] = embodiment
        if role is not None:
            body["role"] = role
        # T-handoff-target-structured-field msg-068 §4-1. See the twin
        # comment on ``open_thread``.
        if next_participant is not None:
            body["next_participant"] = next_participant
        return await self._request_json(
            "POST",
            f"/v1/projects/{project}/threads/{thread_id}/messages",
            json=body,
        )

    async def close_thread(
        self,
        *,
        project: str,
        thread_id: str,
        summary_content: str,
        author: str,
        affects_threads: list[str] | None = None,
        related_tasks: list[str] | None = None,
        tags: list[str] | None = None,
        commit_ref: str | None = None,
        timestamp: datetime | str | None = None,
        embodiment: str | None = None,
        role: str | None = None,
        owner_override: bool = False,
        owner_override_reason: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "summary_content": summary_content,
            "author": author,
        }
        if owner_override:
            body["owner_override"] = True
        if owner_override_reason is not None:
            body["owner_override_reason"] = owner_override_reason
        if affects_threads is not None:
            body["affects_threads"] = affects_threads
        if related_tasks is not None:
            body["related_tasks"] = related_tasks
        if tags is not None:
            body["tags"] = tags
        if commit_ref is not None:
            body["commit_ref"] = commit_ref
        if timestamp is not None:
            body["timestamp"] = self._isoformat(timestamp)
        if embodiment is not None:
            body["embodiment"] = embodiment
        if role is not None:
            body["role"] = role
        return await self._request_json(
            "POST",
            f"/v1/projects/{project}/threads/{thread_id}/close",
            json=body,
        )

    # --- read endpoints -------------------------------------------------

    async def list_threads(
        self,
        *,
        project: str,
        status_filter: list[str] | None = None,
        owner: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        params: list[tuple[str, str | int]] = [("limit", limit), ("offset", offset)]
        if status_filter:
            for s in status_filter:
                params.append(("status", s))
        if owner:
            params.append(("owner", owner))
        return await self._request_json(
            "GET", f"/v1/projects/{project}/threads", params=params
        )

    async def list_project_summaries(self) -> dict[str, Any]:
        """Per-project thread counts across every project, in one request.

        The cross-project counterpart to ``list_threads``. Used by the
        dashboard, which needs to rank projects rather than read any one
        of them; going through ``list_threads`` would cost a round trip
        per project and still not say which projects exist.
        """
        return await self._request_json("GET", "/v1/projects")

    async def get_thread(
        self,
        *,
        project: str,
        thread_id: str,
        mode: str = "full",
        include_digest: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"mode": mode}
        if include_digest:
            # Orthogonal to `mode`. `mode=summary` filters the *messages*
            # (decide-only on a resolved thread); the digest is a separate
            # object. Only sent when asked, so nothing changes for the
            # callers that do not read it.
            params["include_digest"] = "true"
        return await self._request_json(
            "GET",
            f"/v1/projects/{project}/threads/{thread_id}",
            params=params,
        )

    # --- thread digests (LLM summaries) ---------------------------------
    #
    # Magickit is the producer and Conclair the store: the digest is
    # computed here (Cognilens -> Lexora light) and PUT back. That is the
    # only allowed direction -- Conclair must stay a leaf that calls
    # nothing, which is also why it cannot make a digest itself.

    async def get_thread_digest(
        self,
        *,
        project: str,
        thread_id: str,
        scope: str = "thread",
        target_msg_id: str | None = None,
        style: str | None = None,
    ) -> dict[str, Any]:
        """Read a thread's stored digest and how far behind it is.

        A thread with no digest yet answers **200** with
        ``present: false``, not an error envelope. Branch on ``present``:
        reading "not digested yet" as an outage means never digesting
        anything, and reading an outage as "not digested yet" costs one
        light-tier call. Conclair 404s only when the *thread* is missing.

        Args:
            project: Project identifier.
            thread_id: Thread whose digest to read.
            scope: ``thread`` for the whole-thread digest, ``message`` for one msg.
            target_msg_id: The msg, when ``scope`` is ``message``.
            style: Which digest to read; producers may store several.

        Returns:
            Conclair's ``ThreadDigestResponse``, or its error envelope.
        """
        params: dict[str, Any] = {"scope": scope}
        if target_msg_id is not None:
            params["target_msg_id"] = target_msg_id
        if style is not None:
            params["style"] = style
        return await self._request_json(
            "GET",
            f"/v1/projects/{project}/threads/{thread_id}/digest",
            params=params,
        )

    async def put_thread_digest(
        self,
        *,
        project: str,
        thread_id: str,
        digest: str,
        source_last_msg_id: str,
        source_msg_count: int,
        producer: str,
        scope: str = "thread",
        target_msg_id: str | None = None,
        style: str | None = None,
        truncated: bool | None = None,
        model: str | None = None,
        tier: str | None = None,
        source_chars: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        """Store (upsert) a finished digest.

        ``source_last_msg_id`` is the freshness key: messages are
        append-only, so the digest is stale iff it differs from the
        thread's current ``last_msg_id``. **It must come from the same
        read the digest was computed from** -- reusing an id from an
        earlier listing marks the digest as covering messages it never
        saw. Conclair rejects an id that is not in this thread (409),
        which also catches a sibling thread's id (``msg_id`` is allocated
        project-wide) and an over-padded one.

        Args:
            project: Project identifier.
            thread_id: Thread the digest describes.
            digest: The summary text.
            source_last_msg_id: Newest msg the digest covers.
            source_msg_count: Msgs the producer actually read (provenance;
                Conclair derives staleness from the log, not from this).
            producer: Who wrote it. A record, not a credential.
            scope: ``thread`` or ``message``.
            target_msg_id: The msg, when ``scope`` is ``message``.
            style: Style label; part of the storage key.
            truncated: The producer shortened the input before summarizing.
            model: Model that served the request, when observable.
            tier: Lexora tier that was *requested*.
            source_chars: Characters actually fed to the model.
            input_tokens: Prompt tokens, when reported.
            output_tokens: Completion tokens, when reported.
            duration_ms: Wall clock for the summarize call.

        Returns:
            Conclair's ``ThreadDigestResponse``, or its error envelope.
        """
        body: dict[str, Any] = {
            "digest": digest,
            "source_last_msg_id": source_last_msg_id,
            "source_msg_count": source_msg_count,
            "producer": producer,
            "scope": scope,
        }
        # Optional fields are omitted rather than sent as null so Conclair's
        # own defaults apply -- the same convention as `mark_read`.
        if target_msg_id is not None:
            body["target_msg_id"] = target_msg_id
        if style is not None:
            body["style"] = style
        if truncated is not None:
            body["truncated"] = truncated
        if model is not None:
            body["model"] = model
        if tier is not None:
            body["tier"] = tier
        if source_chars is not None:
            body["source_chars"] = source_chars
        if input_tokens is not None:
            body["input_tokens"] = input_tokens
        if output_tokens is not None:
            body["output_tokens"] = output_tokens
        if duration_ms is not None:
            body["duration_ms"] = duration_ms
        return await self._request_json(
            "PUT",
            f"/v1/projects/{project}/threads/{thread_id}/digest",
            json=body,
        )

    async def list_events(
        self,
        *,
        project: str,
        thread_id: str | None = None,
        action: str | None = None,
        since: datetime | str | None = None,
        until: datetime | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        params: list[tuple[str, str | int]] = [("limit", limit), ("offset", offset)]
        if thread_id:
            params.append(("thread_id", thread_id))
        if action:
            params.append(("action", action))
        if since is not None:
            iso = self._isoformat(since)
            assert iso is not None
            params.append(("since", iso))
        if until is not None:
            iso = self._isoformat(until)
            assert iso is not None
            params.append(("until", iso))
        return await self._request_json(
            "GET", f"/v1/projects/{project}/events", params=params
        )

    async def check_integrity(self, *, project: str) -> dict[str, Any]:
        return await self._request_json(
            "GET", f"/v1/projects/{project}/integrity"
        )

    # --- read cursor (per-identity inbox / mark_read) -------------------

    async def mark_read(
        self,
        *,
        project: str,
        thread_id: str,
        identity_name: str,
        up_to_msg_id: str | None = None,
    ) -> dict[str, Any]:
        """Advance ``identity_name``'s read cursor on ``thread_id``.

        ``up_to_msg_id=None`` advances to the thread's current latest msg
        (catch-up shortcut). The endpoint is monotonic-forward only: a
        request pointing at the current cursor or earlier returns
        ``advanced=False`` without writing.
        """
        body: dict[str, Any] = {"identity_name": identity_name}
        # Forward the field only when supplied so the catch-up case
        # serializes as a JSON object without a stray ``"up_to_msg_id":
        # null`` -- the server's Pydantic schema treats absent and null
        # the same way (both advance to latest), but the cleaner payload
        # matches the existing thin-wrapper pattern in this adapter.
        if up_to_msg_id is not None:
            body["up_to_msg_id"] = up_to_msg_id
        return await self._request_json(
            "POST",
            f"/v1/projects/{project}/threads/{thread_id}/read",
            json=body,
        )

    async def list_unread(
        self,
        *,
        project: str,
        identity_name: str,
        include_resolved: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Inbox: threads with at least one msg this identity has not read.

        Sort order on the server is "most unread first", so the first
        page is already actionable.
        """
        params: list[tuple[str, str | int]] = [
            ("identity_name", identity_name),
            ("include_resolved", "true" if include_resolved else "false"),
            ("limit", limit),
            ("offset", offset),
        ]
        return await self._request_json(
            "GET", f"/v1/projects/{project}/unread", params=params,
        )

    # --- loop control (HOLD / RESUME) ------------------------------------
    #
    # Two writers, two methods, on purpose. `set_loop_control` is the
    # operator's; `report_loop_control_observed` is the loop's. Conclair
    # keeps the two in separate columns, and collapsing them here would
    # put the "a loop can resume itself" failure back one layer up.

    async def get_loop_control(self, *, project: str) -> dict[str, Any]:
        """Read the project's loop control state.

        Never 404s upstream: an unconfigured project answers 200 with
        ``configured=false`` and the ``run`` default. An error envelope
        from here therefore means the read genuinely failed, and callers
        must treat that as ``hold`` rather than fall back to a default.
        """
        return await self._request_json("GET", f"/v1/projects/{project}/control")

    async def set_loop_control(
        self,
        *,
        project: str,
        state: str,
        actor: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Set the *desired* state. Operator action; records ``actor``."""
        body: dict[str, Any] = {"state": state, "actor": actor}
        if note is not None:
            body["note"] = note
        return await self._request_json(
            "PUT", f"/v1/projects/{project}/control", json=body
        )

    async def report_loop_control_observed(
        self,
        *,
        project: str,
        state: str,
        actor: str,
    ) -> dict[str, Any]:
        """Report the state the loop observed. Never touches ``desired``."""
        return await self._request_json(
            "POST",
            f"/v1/projects/{project}/control/observed",
            json={"state": state, "actor": actor},
        )
