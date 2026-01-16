"""Authentication and authorization module."""

from magickit.auth.dependencies import get_current_user, get_optional_user
from magickit.auth.jwt import JWTHandler
from magickit.auth.permissions import Permission, require_permission

__all__ = [
    "JWTHandler",
    "get_current_user",
    "get_optional_user",
    "Permission",
    "require_permission",
]
