"""The repo's ``config/magickit_config.yaml`` names the settled-disposition closer.

mindwire T-done-disposition-auto-close msg-8589 (Tier-C) / msg-8612 O-2: the closer
allowlist lives in the git-tracked config, which production reads after a fast-forward
deploy. This test reads that file itself, so removing or misspelling the entry fails CI
instead of silently leaving every settled close ``closer_not_configured``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from magickit.config import Settings

_REPO_CONFIG = Path(__file__).resolve().parents[2] / "config" / "magickit_config.yaml"


def test_repo_config_names_the_settled_closer_with_the_default_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The yaml wins over these env vars for closers, but grace_seconds is deliberately not
    # in the yaml, so a stray env value on the test host would change it. Clear both.
    monkeypatch.delenv("MAGICKIT_SETTLED_CLOSE_CLOSERS", raising=False)
    monkeypatch.delenv("MAGICKIT_SETTLED_CLOSE_GRACE_SECONDS", raising=False)

    settings = Settings.from_yaml(_REPO_CONFIG)

    # The name must equal mindwire identity/classification.py::SETTLED_CLOSER exactly.
    assert settings.settled_close_closers == ["settled-closer"]
    assert settings.settled_close_grace_seconds == 86400
