"""JWT token handling for authentication."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from magickit.utils.logging import get_logger

logger = get_logger(__name__)


class JWTHandler:
    """Handles JWT token creation and verification."""

    def __init__(
        self,
        secret_key: str,
        algorithm: str = "HS256",
        access_token_expire_minutes: int = 60,
        refresh_token_expire_days: int = 7,
    ) -> None:
        """Initialize JWT handler.

        Args:
            secret_key: Secret key for signing tokens.
            algorithm: JWT signing algorithm.
            access_token_expire_minutes: Access token expiry in minutes.
            refresh_token_expire_days: Refresh token expiry in days.
        """
        self.secret_key = secret_key
        self.algorithm = algorithm
        self.access_token_expire_minutes = access_token_expire_minutes
        self.refresh_token_expire_days = refresh_token_expire_days
        self._pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

    def hash_password(self, password: str) -> str:
        """Hash a password using bcrypt.

        Args:
            password: Plain text password.

        Returns:
            Hashed password.
        """
        return self._pwd_context.hash(password)

    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        """Verify a password against its hash.

        Args:
            plain_password: Plain text password.
            hashed_password: Hashed password.

        Returns:
            True if password matches, False otherwise.
        """
        return self._pwd_context.verify(plain_password, hashed_password)

    def create_access_token(
        self,
        user_id: str,
        email: str,
        role: str,
        additional_claims: dict[str, Any] | None = None,
    ) -> str:
        """Create an access token.

        Args:
            user_id: User ID to encode.
            email: User email.
            role: User role.
            additional_claims: Additional JWT claims.

        Returns:
            Encoded JWT token.
        """
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=self.access_token_expire_minutes
        )
        payload = {
            "sub": user_id,
            "email": email,
            "role": role,
            "type": "access",
            "exp": expire,
            "iat": datetime.now(timezone.utc),
        }
        if additional_claims:
            payload.update(additional_claims)

        return jwt.encode(payload, self.secret_key, algorithm=self.algorithm)

    def create_refresh_token(self, user_id: str) -> str:
        """Create a refresh token.

        Args:
            user_id: User ID to encode.

        Returns:
            Encoded JWT refresh token.
        """
        expire = datetime.now(timezone.utc) + timedelta(
            days=self.refresh_token_expire_days
        )
        payload = {
            "sub": user_id,
            "type": "refresh",
            "exp": expire,
            "iat": datetime.now(timezone.utc),
        }

        return jwt.encode(payload, self.secret_key, algorithm=self.algorithm)

    def decode_token(self, token: str) -> dict[str, Any] | None:
        """Decode and verify a JWT token.

        Args:
            token: JWT token to decode.

        Returns:
            Decoded payload if valid, None otherwise.
        """
        try:
            payload = jwt.decode(token, self.secret_key, algorithms=[self.algorithm])
            return payload
        except JWTError as e:
            logger.warning("jwt_decode_error", error=str(e))
            return None

    def verify_access_token(self, token: str) -> dict[str, Any] | None:
        """Verify an access token.

        Args:
            token: JWT access token.

        Returns:
            Decoded payload if valid access token, None otherwise.
        """
        payload = self.decode_token(token)
        if payload is None:
            return None

        if payload.get("type") != "access":
            logger.warning("invalid_token_type", expected="access", got=payload.get("type"))
            return None

        return payload

    def verify_refresh_token(self, token: str) -> str | None:
        """Verify a refresh token and extract user ID.

        Args:
            token: JWT refresh token.

        Returns:
            User ID if valid refresh token, None otherwise.
        """
        payload = self.decode_token(token)
        if payload is None:
            return None

        if payload.get("type") != "refresh":
            logger.warning("invalid_token_type", expected="refresh", got=payload.get("type"))
            return None

        return payload.get("sub")

    def get_token_expiry_seconds(self) -> int:
        """Get access token expiry in seconds.

        Returns:
            Expiry time in seconds.
        """
        return self.access_token_expire_minutes * 60
