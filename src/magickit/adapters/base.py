"""Base adapter class for external services."""

from abc import ABC, abstractmethod
from typing import Any

import httpx

from magickit.utils.logging import get_logger

logger = get_logger(__name__)


class BaseAdapter(ABC):
    """Abstract base class for service adapters.

    All adapters must implement health_check and can optionally
    override other methods for service-specific functionality.
    """

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        """Initialize the adapter.

        Args:
            base_url: Base URL of the service.
            timeout: Request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout),
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "BaseAdapter":
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit."""
        await self.close()

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the service is healthy.

        Returns:
            True if the service is healthy, False otherwise.
        """
        pass

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an HTTP request to the service.

        Args:
            method: HTTP method (GET, POST, etc.).
            path: Request path.
            **kwargs: Additional arguments passed to httpx.

        Returns:
            HTTP response.

        Raises:
            httpx.HTTPError: If the request fails.
        """
        logger.debug(
            "Making request",
            method=method,
            url=f"{self.base_url}{path}",
        )
        response = await self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    async def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        """Make a GET request."""
        return await self._request("GET", path, **kwargs)

    async def _post(self, path: str, **kwargs: Any) -> httpx.Response:
        """Make a POST request."""
        return await self._request("POST", path, **kwargs)
