"""User identification utilities for multi-user support.

Provides automatic user identification with fallback chain:
1. Environment variable SPIRROW_USER
2. Git config user.email
3. OS username
"""

import getpass
import os
import subprocess
from functools import lru_cache

from magickit.utils.logging import get_logger

logger = get_logger(__name__)


@lru_cache(maxsize=1)
def get_git_user_email() -> str | None:
    """Get user email from git config.

    Returns:
        Git user email or None if not configured.
    """
    try:
        result = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.debug("Failed to get git user.email", error=str(e))
    return None


@lru_cache(maxsize=1)
def get_os_username() -> str:
    """Get OS username.

    Returns:
        OS username.
    """
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def get_current_user() -> str:
    """Get current user identifier using fallback chain.

    Priority:
    1. SPIRROW_USER environment variable
    2. Git config user.email
    3. OS username

    Returns:
        User identifier string.
    """
    # 1. Check environment variable
    env_user = os.environ.get("SPIRROW_USER", "").strip()
    if env_user:
        logger.debug("Using user from SPIRROW_USER env", user=env_user)
        return env_user

    # 2. Try git config
    git_email = get_git_user_email()
    if git_email:
        logger.debug("Using user from git config", user=git_email)
        return git_email

    # 3. Fall back to OS username
    os_user = get_os_username()
    logger.debug("Using OS username", user=os_user)
    return os_user


def clear_user_cache() -> None:
    """Clear cached user information.

    Call this if environment or git config changes during runtime.
    """
    get_git_user_email.cache_clear()
    get_os_username.cache_clear()
