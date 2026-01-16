"""Authentication middleware for FastAPI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from magickit.utils.logging import get_logger

if TYPE_CHECKING:
    from magickit.auth.jwt import JWTHandler

logger = get_logger(__name__)


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware to extract and validate JWT tokens from requests.

    Attaches user information to request.state if token is valid.
    Does not block requests without tokens (that's handled by dependencies).
    """

    # Paths that don't need authentication
    PUBLIC_PATHS = {
        "/",
        "/health",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/auth/register",
        "/auth/login",
        "/auth/refresh",
        "/dashboard",
        "/static",
    }

    def __init__(
        self,
        app: object,
        jwt_handler: JWTHandler,
        auth_enabled: bool = True,
    ) -> None:
        """Initialize auth middleware.

        Args:
            app: FastAPI application.
            jwt_handler: JWT handler instance.
            auth_enabled: Whether authentication is enabled.
        """
        super().__init__(app)  # type: ignore[arg-type]
        self.jwt_handler = jwt_handler
        self.auth_enabled = auth_enabled

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Process request and extract user info from token.

        Args:
            request: Incoming request.
            call_next: Next middleware/handler.

        Returns:
            Response from downstream handler.
        """
        # Initialize user state
        request.state.user = None
        request.state.user_id = None

        # Skip auth for public paths
        if not self.auth_enabled or self._is_public_path(request.url.path):
            return await call_next(request)

        # Extract token from Authorization header
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]  # Remove "Bearer " prefix
            payload = self.jwt_handler.verify_access_token(token)

            if payload:
                request.state.user = payload
                request.state.user_id = payload.get("sub")
                logger.debug(
                    "auth_user_identified",
                    user_id=request.state.user_id,
                    path=request.url.path,
                )

        return await call_next(request)

    def _is_public_path(self, path: str) -> bool:
        """Check if path is public (doesn't require auth).

        Args:
            path: Request path.

        Returns:
            True if public, False otherwise.
        """
        # Exact match
        if path in self.PUBLIC_PATHS:
            return True

        # Prefix match for static files and WebSocket
        for prefix in ["/static/", "/ws/", "/dashboard"]:
            if path.startswith(prefix):
                return True

        return False
