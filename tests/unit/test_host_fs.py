"""Opening a registered file without following anything (msg-1757 B-6, M4).

Real filesystem operations under a private temporary directory. The walk
starts at that directory (``base``) because pytest's temporaries live under
the world-writable ``/tmp``, which the walk refuses by design.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX filesystem semantics")

from magickit.hosts import fs  # noqa: E402


@pytest.fixture
def base(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "svc" / "shared").mkdir(parents=True)
    os.chmod(root, 0o700)
    (root / "svc" / "shared" / ".env").write_text("A=1\n")
    (root / "outside").mkdir()
    (root / "outside" / ".env").write_text("STOLEN=1\n")
    return root


def read(base: Path, rel: str, max_bytes: int = 1024) -> bytes:
    return fs.read_registered(
        PurePosixPath(str(base / rel)), max_bytes=max_bytes, base=PurePosixPath(str(base))
    ).data


def test_reads_a_plain_file(base):
    assert read(base, "svc/shared/.env") == b"A=1\n"


def test_a_symlinked_file_is_refused(base):
    (base / "svc" / "shared" / "link.env").symlink_to(base / "outside" / ".env")
    with pytest.raises(fs.UnsafePathError):
        read(base, "svc/shared/link.env")


def test_a_symlinked_parent_is_refused(base):
    (base / "alias").symlink_to(base / "svc")
    with pytest.raises(fs.UnsafePathError):
        read(base, "alias/shared/.env")


def test_a_parent_swapped_for_a_symlink_after_registration_is_refused(base):
    # Registered while it was a real directory...
    assert read(base, "svc/shared/.env") == b"A=1\n"
    # ...then someone replaces the parent with a link to somewhere else
    # holding a file of the same name (Einstein msg-1758 Obj-6).
    (base / "svc" / "shared").rename(base / "svc" / "shared.old")
    (base / "svc" / "shared").symlink_to(base / "outside")
    with pytest.raises(fs.UnsafePathError):
        read(base, "svc/shared/.env")


def test_world_writable_components_are_refused(base):
    os.chmod(base / "svc", 0o777)
    with pytest.raises(fs.UnsafePathError) as err:
        read(base, "svc/shared/.env")
    assert str(base) not in str(err.value)  # no paths in what callers see


def test_a_world_writable_file_is_refused(base):
    os.chmod(base / "svc" / "shared" / ".env", 0o666)
    with pytest.raises(fs.UnsafePathError):
        read(base, "svc/shared/.env")


def test_a_fifo_is_refused_without_blocking(base):
    os.mkfifo(base / "svc" / "shared" / "fifo")
    with pytest.raises(fs.UnsafePathError):
        read(base, "svc/shared/fifo")


def test_size_is_capped(base):
    (base / "svc" / "shared" / "big").write_bytes(b"x" * 2000)
    with pytest.raises(fs.UnsafePathError):
        read(base, "svc/shared/big", max_bytes=1000)


def test_dotdot_components_are_refused(base):
    # ("." never reaches the walk: PurePosixPath drops it, leaving the same path.)
    path = PurePosixPath(str(base) + "/svc/../outside/.env")
    with pytest.raises(fs.UnsafePathError):
        fs.read_registered(path, max_bytes=1024, base=PurePosixPath(str(base)))


def test_a_path_outside_the_base_is_refused(base, tmp_path):
    with pytest.raises(fs.UnsafePathError):
        fs.read_registered(
            PurePosixPath(str(tmp_path / "x")), max_bytes=10, base=PurePosixPath(str(base))
        )


def test_the_real_walk_from_root_refuses_tmp():
    # /tmp is world-writable (sticky): anything under it fails the walk
    # from "/" -- which is why the tests above pass `base`.
    with pytest.raises(fs.UnsafePathError):
        fs.read_registered(PurePosixPath("/tmp/anything"), max_bytes=10)
