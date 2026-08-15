"""Pinning the working tree, against real git repositories.

These build actual repos in ``tmp_path`` rather than mocking
``subprocess``. A mocked git proves the code calls the commands the
author expected; the thing worth proving is that after this runs, the
tree is on the commit that was approved and on nothing else -- which
only git can answer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from magickit.deploy import pin as pin_mod
from magickit.deploy.pin import PinRefusedError


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _commit(repo: Path, name: str, body: str = "x") -> str:
    (repo / name).write_text(body)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def remote_and_clone(tmp_path) -> tuple[Path, Path]:
    """An upstream with two commits on main, and a clone on the first."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "t@example.com")
    _git(upstream, "config", "user.name", "t")
    first = _commit(upstream, "one.txt")

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(clone)], check=True, capture_output=True
    )
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")

    second = _commit(upstream, "two.txt")
    assert first != second
    return upstream, clone


# ── the happy path ───────────────────────────────────────────────


def test_pin_fast_forwards_the_local_branch_to_origin_main(remote_and_clone):
    upstream, clone = remote_and_clone
    wanted = _git(upstream, "rev-parse", "HEAD")

    result = pin_mod.pin(clone, "origin/main")

    assert result.sha == wanted
    assert _git(clone, "rev-parse", "HEAD") == wanted
    assert result.changed is True
    # Production stays on a branch: the next person to log in sees what
    # they expect, not a detached HEAD.
    assert result.detached is False
    assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert (clone / "two.txt").exists()


def test_pinning_an_already_current_tree_is_a_no_op_that_still_reports_the_sha(
    remote_and_clone,
):
    _, clone = remote_and_clone
    first = pin_mod.pin(clone, "origin/main")
    again = pin_mod.pin(clone, "origin/main")

    assert again.sha == first.sha
    assert again.changed is False


def test_an_override_ref_is_checked_out_detached(remote_and_clone):
    upstream, clone = remote_and_clone
    _git(upstream, "checkout", "-q", "-b", "feat/x")
    override_sha = _commit(upstream, "three.txt")

    result = pin_mod.pin(clone, "origin/feat/x")

    assert result.sha == override_sha
    assert result.detached is True
    assert _git(clone, "rev-parse", "HEAD") == override_sha
    # An override is an exceptional state and should look like one.
    assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


# ── the refusals, all before anything is restarted ───────────────


def test_a_dirty_production_tree_is_refused_not_stashed(remote_and_clone):
    _, clone = remote_and_clone
    (clone / "one.txt").write_text("someone was editing this")
    before = _git(clone, "rev-parse", "HEAD")

    with pytest.raises(PinRefusedError, match="local modifications"):
        pin_mod.pin(clone, "origin/main")

    # Nothing moved, and their edit is still there.
    assert _git(clone, "rev-parse", "HEAD") == before
    assert (clone / "one.txt").read_text() == "someone was editing this"


def test_an_unknown_ref_is_refused(remote_and_clone):
    _, clone = remote_and_clone
    with pytest.raises(PinRefusedError, match="cannot resolve"):
        pin_mod.pin(clone, "origin/no-such-branch")


def test_a_non_fast_forward_is_refused_rather_than_resolved(remote_and_clone):
    """Local commits on main mean someone deployed by hand, or a deploy
    died halfway. Merging that automatically during a deploy is exactly
    the wrong moment to be clever."""
    _, clone = remote_and_clone
    _commit(clone, "local-only.txt")
    before = _git(clone, "rev-parse", "HEAD")

    with pytest.raises(PinRefusedError):
        pin_mod.pin(clone, "origin/main")

    assert _git(clone, "rev-parse", "HEAD") == before


def test_a_non_repository_is_refused(tmp_path):
    with pytest.raises(PinRefusedError, match="not a git working tree"):
        pin_mod.pin(tmp_path, "origin/main")


# ── the R-2 gate's question ──────────────────────────────────────


def test_matches_remote_main_is_true_only_for_the_exact_commit(remote_and_clone):
    _, clone = remote_and_clone
    result = pin_mod.pin(clone, "origin/main")

    assert pin_mod.matches_remote_main(clone, result.sha, default_ref="origin/main") is True
    assert (
        pin_mod.matches_remote_main(clone, result.previous_sha, default_ref="origin/main") is False
    )


def test_matches_remote_main_is_false_for_an_override_commit(remote_and_clone):
    upstream, clone = remote_and_clone
    _git(upstream, "checkout", "-q", "-b", "feat/x")
    _commit(upstream, "three.txt")

    result = pin_mod.pin(clone, "origin/feat/x")

    # This is what shuts the migration gate for an overridden ref.
    assert pin_mod.matches_remote_main(clone, result.sha, default_ref="origin/main") is False


def test_matches_remote_main_is_false_when_the_ref_cannot_be_resolved(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    sha = _commit(repo, "one.txt")

    # No origin at all -> the gate fails closed rather than raising.
    assert pin_mod.matches_remote_main(repo, sha, default_ref="origin/main") is False


def test_head_sha_reads_the_tree_not_the_request(remote_and_clone):
    _, clone = remote_and_clone
    assert pin_mod.head_sha(clone) == _git(clone, "rev-parse", "HEAD")
