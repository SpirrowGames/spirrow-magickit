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
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": type,
            "author": author,
            "content": content,
        }
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
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "summary_content": summary_content,
            "author": author,
        }
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

    async def get_thread(
        self,
        *,
        project: str,
        thread_id: str,
        mode: str = "full",
    ) -> dict[str, Any]:
        return await self._request_json(
            "GET",
            f"/v1/projects/{project}/threads/{thread_id}",
            params={"mode": mode},
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
