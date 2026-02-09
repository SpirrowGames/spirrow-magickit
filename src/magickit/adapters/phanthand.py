"""Adapter for Phanthand file access API on development PCs.

Unlike other adapters, PhanthandAdapter does NOT inherit from BaseAdapter
because Phanthand instances are per-developer. URL and API key are passed
at call time rather than configured at initialization.
"""

from __future__ import annotations

from typing import Any

import httpx

from magickit.utils.logging import get_logger

logger = get_logger(__name__)


# ── Exceptions ────────────────────────────────────────────────────


class PhanthandError(Exception):
    """Base exception for Phanthand operations."""


class PhanthandConnectionError(PhanthandError):
    """Cannot connect to Phanthand server."""


class PhanthandAuthError(PhanthandError):
    """Authentication failed."""


class PhanthandPathNotAllowedError(PhanthandError):
    """Path is outside allowed directories."""


class PhanthandFileNotFoundError(PhanthandError):
    """File or directory not found."""


class PhanthandRequestError(PhanthandError):
    """General request error."""


class PhanthandTimeoutError(PhanthandError):
    """Request timed out."""


# ── Adapter ───────────────────────────────────────────────────────


class PhanthandAdapter:
    """File access API client for development PCs.

    Provides read-only file operations via Phanthand HTTP API.
    Each method receives url and api_key as arguments, allowing
    different developers to use their own Phanthand instances.

    Args:
        timeout: Request timeout in seconds.
    """

    def __init__(self, timeout: float = 30.0) -> None:
        """Initialize the adapter.

        Args:
            timeout: Default request timeout in seconds.
        """
        self._timeout = timeout

    async def _request(
        self,
        url: str,
        api_key: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        auth_required: bool = True,
    ) -> dict[str, Any]:
        """Send a request to Phanthand.

        Args:
            url: Phanthand base URL (e.g., "http://192.168.1.10:7300").
            api_key: Bearer token for authentication.
            endpoint: API endpoint path (e.g., "/files/read").
            payload: JSON request body.
            auth_required: Whether to include auth header.

        Returns:
            Parsed response data (the "data" field from ApiResponse).

        Raises:
            PhanthandConnectionError: Cannot reach Phanthand.
            PhanthandAuthError: Invalid API key.
            PhanthandPathNotAllowedError: Path outside allowed directories.
            PhanthandFileNotFoundError: File or directory not found.
            PhanthandRequestError: Other request errors.
            PhanthandTimeoutError: Request timed out.
        """
        base_url = url.rstrip("/")
        full_url = f"{base_url}{endpoint}"

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if auth_required:
            headers["Authorization"] = f"Bearer {api_key}"

        logger.debug(
            "Phanthand request",
            url=full_url,
            endpoint=endpoint,
        )

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
            ) as client:
                response = await client.post(
                    full_url,
                    json=payload or {},
                    headers=headers,
                )
        except httpx.ConnectError as e:
            raise PhanthandConnectionError(
                f"Cannot connect to Phanthand at {base_url}: {e}"
            ) from e
        except httpx.TimeoutException as e:
            raise PhanthandTimeoutError(
                f"Request to Phanthand timed out: {e}"
            ) from e
        except httpx.HTTPError as e:
            raise PhanthandRequestError(
                f"HTTP error communicating with Phanthand: {e}"
            ) from e

        # Handle HTTP error status codes
        if response.status_code == 401:
            raise PhanthandAuthError("Invalid API key for Phanthand")
        if response.status_code == 403:
            detail = self._extract_error(response)
            raise PhanthandPathNotAllowedError(
                detail or "Path is outside allowed directories"
            )
        if response.status_code == 404:
            detail = self._extract_error(response)
            raise PhanthandFileNotFoundError(
                detail or "File or directory not found"
            )

        if response.status_code >= 400:
            detail = self._extract_error(response)
            raise PhanthandRequestError(
                f"Phanthand error ({response.status_code}): {detail}"
            )

        # Parse ApiResponse format: {"success": bool, "data": ..., "error": ...}
        body = response.json()

        if not body.get("success", False):
            error_msg = body.get("error", "Unknown error")
            raise PhanthandRequestError(f"Phanthand returned error: {error_msg}")

        return body.get("data", {})

    def _extract_error(self, response: httpx.Response) -> str:
        """Extract error message from a Phanthand response.

        Args:
            response: The HTTP response.

        Returns:
            Error message string.
        """
        try:
            body = response.json()
            return body.get("error", "") or body.get("detail", "")
        except Exception:
            return response.text[:200]

    # ── File Operations ───────────────────────────────────────────

    async def health_check(self, url: str) -> dict[str, Any]:
        """Check if Phanthand is running.

        Args:
            url: Phanthand base URL.

        Returns:
            Health data dict with status, version, hostname, uptime_seconds.
        """
        base_url = url.rstrip("/")
        full_url = f"{base_url}/health"

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
            ) as client:
                response = await client.get(full_url)

            body = response.json()
            return body.get("data", {})
        except Exception as e:
            raise PhanthandConnectionError(
                f"Phanthand health check failed at {base_url}: {e}"
            ) from e

    async def read_file(
        self,
        url: str,
        api_key: str,
        path: str,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        """Read a text file.

        Args:
            url: Phanthand base URL.
            api_key: Bearer token.
            path: Absolute file path on the development PC.
            encoding: Text encoding.

        Returns:
            Dict with path, content, size, encoding.
        """
        return await self._request(url, api_key, "/files/read", {
            "path": path,
            "encoding": encoding,
        })

    async def list_directory(
        self,
        url: str,
        api_key: str,
        path: str,
        pattern: str = "*",
        recursive: bool = False,
    ) -> dict[str, Any]:
        """List directory contents.

        Args:
            url: Phanthand base URL.
            api_key: Bearer token.
            path: Absolute directory path.
            pattern: Glob pattern for filtering.
            recursive: Whether to search subdirectories.

        Returns:
            Dict with path, entries list, count.
        """
        return await self._request(url, api_key, "/files/list", {
            "path": path,
            "pattern": pattern,
            "recursive": recursive,
        })

    async def file_exists(
        self,
        url: str,
        api_key: str,
        path: str,
    ) -> dict[str, Any]:
        """Check if a file or directory exists.

        Args:
            url: Phanthand base URL.
            api_key: Bearer token.
            path: Absolute path to check.

        Returns:
            Dict with path, exists, is_file, is_dir.
        """
        return await self._request(url, api_key, "/files/exists", {
            "path": path,
        })

    async def file_info(
        self,
        url: str,
        api_key: str,
        path: str,
    ) -> dict[str, Any]:
        """Get file metadata.

        Args:
            url: Phanthand base URL.
            api_key: Bearer token.
            path: Absolute file path.

        Returns:
            Dict with path, name, size, created, modified, is_file, is_dir, readonly.
        """
        return await self._request(url, api_key, "/files/info", {
            "path": path,
        })

    async def tree(
        self,
        url: str,
        api_key: str,
        path: str,
        max_depth: int = 3,
        exclude_patterns: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get recursive directory tree.

        Args:
            url: Phanthand base URL.
            api_key: Bearer token.
            path: Absolute directory path.
            max_depth: Maximum depth to traverse.
            exclude_patterns: Directory names to exclude.

        Returns:
            Dict with path and tree structure.
        """
        payload: dict[str, Any] = {
            "path": path,
            "max_depth": max_depth,
        }
        if exclude_patterns is not None:
            payload["exclude_patterns"] = exclude_patterns

        return await self._request(url, api_key, "/files/tree", payload)

    async def search(
        self,
        url: str,
        api_key: str,
        path: str,
        pattern: str,
        max_results: int = 100,
    ) -> dict[str, Any]:
        """Search for files matching a glob pattern.

        Args:
            url: Phanthand base URL.
            api_key: Bearer token.
            path: Root directory to search from.
            pattern: Glob pattern (e.g., "*.py", "**/*.h").
            max_results: Maximum number of results.

        Returns:
            Dict with path, pattern, matches list, count, truncated.
        """
        return await self._request(url, api_key, "/files/search", {
            "path": path,
            "pattern": pattern,
            "max_results": max_results,
        })
