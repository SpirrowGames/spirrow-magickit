"""Open a registered file without following anything (msg-1757 B-6, msg-1759 Obj-1-6).

``O_NOFOLLOW`` on one ``open()`` of a multi-component path protects only
the last component (Einstein msg-1758 Obj-6): a parent directory swapped
for a symlink after registration still redirects the open. So the path is
walked one component at a time from ``/``: each directory is opened with
``O_DIRECTORY | O_NOFOLLOW`` relative to the fd of its parent, checked with
``fstat``, and only then used to open the next. A symlink anywhere fails the
read instead of being followed.

Ownership is checked on every component as well: owned by root or by this
process's user, and not writable by anyone else. "Anyone else" includes a
group: a group-writable directory passes only when the group has no members
besides the owner (true of this host's per-user groups; measured
2026-11-25). Otherwise another account could swap a component between two
reads.

Step 3 (the apply runner) writes through the same walk: it keeps the final
directory fd and creates / renames the temporary file relative to it.

POSIX only; everything platform-specific is looked up inside the
functions so that importing this module works on the Windows loop host
(see ``tests/unit/test_deploy_imports_are_lightweight.py`` for why that
matters).
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath


class UnsafePathError(Exception):
    """The registered path cannot be opened safely as it stands."""


@dataclass(frozen=True)
class FileRead:
    """What a safe read returns."""

    data: bytes
    size: int
    mtime_ns: int


def _trusted_writers(st: os.stat_result) -> bool:
    """Can only root or this process's user change what this entry holds?"""
    import grp
    import pwd

    me = os.geteuid()
    if st.st_uid not in (0, me):
        return False
    mode = st.st_mode
    if mode & stat.S_IWOTH:
        # A sticky world-writable directory (/tmp) still lets others create
        # entries in it; none of the registered paths need one.
        return False
    if mode & stat.S_IWGRP:
        if st.st_gid == 0:
            return True
        try:
            group = grp.getgrgid(st.st_gid)
        except KeyError:
            return False
        owner_name = pwd.getpwuid(st.st_uid).pw_name if st.st_uid else "root"
        if any(member != owner_name for member in group.gr_mem):
            return False
        # Members also include every account whose *primary* group it is.
        for entry in pwd.getpwall():
            if entry.pw_gid == st.st_gid and entry.pw_uid != st.st_uid:
                return False
    return True


def read_registered(
    path: PurePosixPath, *, max_bytes: int, base: PurePosixPath = PurePosixPath("/")
) -> FileRead:
    """Read ``path`` by walking it from ``base`` without following symlinks.

    Args:
        path: an absolute registry path.
        max_bytes: refuse anything larger (checked on the opened file, so
            it cannot grow between the check and the read past it).
        base: where the walk starts; ``path`` must be under it. Production
            always walks from ``/``. Tests pass their own temporary
            directory, because pytest's lives under the world-writable
            ``/tmp``, which the walk rightly refuses.

    Raises:
        UnsafePathError: a component is a symlink, missing, not a directory
            / regular file, writable by someone other than root or us, or
            the file is too large.
    """
    if not path.is_absolute() or not base.is_absolute():
        raise UnsafePathError("the path is not absolute")
    try:
        rel = path.relative_to(base)
    except ValueError:
        raise UnsafePathError("the path is not under the walk's base") from None
    parts = ("",) + rel.parts  # parts[0] stands for the base itself
    if len(parts) < 2 or any(p in ("", ".", "..") for p in parts[1:]):
        raise UnsafePathError("the path has an empty, '.' or '..' component")

    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(str(base), dir_flags)
    try:
        if not _trusted_writers(os.fstat(fd)):
            raise UnsafePathError("the walk's base is writable by an untrusted account")
        for i, part in enumerate(parts[1:-1]):
            try:
                nfd = os.open(part, dir_flags, dir_fd=fd)
            except OSError as exc:
                raise UnsafePathError(
                    f"component {i + 1} of the path cannot be opened as a real directory "
                    f"({type(exc).__name__}: errno {exc.errno})"
                ) from None
            os.close(fd)
            fd = nfd
            if not _trusted_writers(os.fstat(fd)):
                raise UnsafePathError(
                    f"component {i + 1} of the path is writable by an untrusted account"
                )
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        try:
            ffd = os.open(parts[-1], file_flags, dir_fd=fd)
        except OSError as exc:
            raise UnsafePathError(
                f"the file cannot be opened without following a link "
                f"({type(exc).__name__}: errno {exc.errno})"
            ) from None
    finally:
        os.close(fd)

    try:
        st = os.fstat(ffd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafePathError("the registered path is not a regular file")
        if not _trusted_writers(st):
            raise UnsafePathError("the file is writable by an untrusted account")
        if st.st_size > max_bytes:
            raise UnsafePathError(f"the file is larger than {max_bytes} bytes")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(ffd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise UnsafePathError(f"the file is larger than {max_bytes} bytes")
        return FileRead(data=data, size=len(data), mtime_ns=st.st_mtime_ns)
    finally:
        os.close(ffd)
