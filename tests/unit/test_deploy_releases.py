"""Two slots and a symlink: reading the layout, and switching it.

Built on real directories and real symlinks. The properties worth
holding are about what happens when the layout is *not* what the code
assumes -- a missing slot, a `current` that points somewhere unexpected
-- because the consequence of guessing wrong is preparing the directory
that is currently serving, in place, which is the exact failure the
layout exists to prevent.
"""

from __future__ import annotations

import os

import pytest

from magickit.deploy import releases
from magickit.deploy.releases import ReleaseLayoutError


@pytest.fixture
def root(tmp_path):
    """A well-formed layout with `a` live."""
    root = tmp_path / "spirrow-cognilens"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    (root / "shared").mkdir()
    (root / "current").symlink_to("a")
    return root


# ── reading it ───────────────────────────────────────────────────


def test_the_standby_is_the_slot_that_is_not_live(root):
    slots = releases.resolve(root)

    assert slots.current == "a"
    assert slots.standby == "b"
    assert slots.current_path == root / "a"
    assert slots.standby_path == root / "b"
    assert slots.shared_path == root / "shared"


def test_it_follows_the_symlink_after_a_switch(root):
    releases.switch(root, "b")

    slots = releases.resolve(root)
    assert slots.current == "b"
    assert slots.standby == "a"


def test_an_unset_up_directory_is_refused(tmp_path):
    with pytest.raises(ReleaseLayoutError, match="not a symlink"):
        releases.resolve(tmp_path)


def test_a_current_pointing_somewhere_unexpected_is_refused(root):
    """Refuse rather than guess: guessing wrong means preparing the
    directory that is currently serving."""
    (root / "current").unlink()
    (root / "current").symlink_to("somewhere-else")

    with pytest.raises(ReleaseLayoutError, match="not one of"):
        releases.resolve(root)


def test_a_missing_slot_is_refused(root):
    (root / "b").rmdir()

    with pytest.raises(ReleaseLayoutError, match="missing"):
        releases.resolve(root)


def test_a_missing_shared_directory_is_refused(root):
    (root / "shared").rmdir()

    with pytest.raises(ReleaseLayoutError, match="shared"):
        releases.resolve(root)


# ── switching it ─────────────────────────────────────────────────


def test_switching_repoints_current(root):
    releases.switch(root, "b")

    assert os.readlink(root / "current") == "b"
    assert (root / "current").resolve() == root / "b"


def test_switching_leaves_no_temporary_link_behind(root):
    releases.switch(root, "b")
    releases.switch(root, "a")

    assert sorted(p.name for p in root.iterdir()) == ["a", "b", "current", "shared"]


def test_switching_to_a_slot_that_does_not_exist_is_refused(root):
    (root / "b").rmdir()

    with pytest.raises(ReleaseLayoutError):
        releases.switch(root, "b")
    # ...and the live one is untouched.
    assert os.readlink(root / "current") == "a"


def test_switching_to_a_name_that_is_not_a_slot_is_refused(root):
    with pytest.raises(ReleaseLayoutError, match="not one of"):
        releases.switch(root, "../../etc")

    assert os.readlink(root / "current") == "a"


def test_the_symlink_is_relative_so_the_tree_can_be_moved(root):
    releases.switch(root, "b")

    assert os.readlink(root / "current") == "b"
    assert not os.path.isabs(os.readlink(root / "current"))


def test_switching_recovers_from_a_leftover_swap_link(root):
    """A runner killed mid-switch leaves the temporary link; the next
    switch must not trip over it."""
    (root / "current.swap").symlink_to("b")

    releases.switch(root, "b")

    assert os.readlink(root / "current") == "b"
    assert not (root / "current.swap").exists()


def test_describe_says_which_is_which(root):
    assert releases.describe(root) == "current=a standby=b"


def test_describe_does_not_raise_on_a_broken_layout(tmp_path):
    """It is used in error messages, where raising would hide the real
    failure."""
    assert "unreadable" in releases.describe(tmp_path)
