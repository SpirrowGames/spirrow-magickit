"""Which hosts, which targets on them, and which handles each target has.

Two levels on purpose (msg-1750 §2.5, R-6): ``host -> {targets}``. v1 has
one host, and every record, tool argument and audit line carries ``host``
from the start, so a second host is a new entry here plus an executor for
it -- not a rewrite.

Like :mod:`magickit.deploy.registry`, the table is Python rather than YAML:
it is the security boundary of the feature, so changing it is a pull
request, not an edit to a file the service re-reads on restart.

What is deliberately *not* here (v1):

- **magickit's own units** (R-4, msg-1757 B-9). Refused by name prefix in
  :func:`_check_unit`, before the table is consulted, so that adding an
  entry cannot switch it on by accident.
- **Executed files** (``start.sh``, scripts, units) in ``env_files``
  (msg-1757 A-1): a change to one is a change to code. ``env_files`` lists
  files that are only *read* as data. Step 1 only lists their key names;
  the per-key type allowlist that config changes need (msg-1759 Obj-1-2)
  arrives with step 2 and is empty until then.
- **Log files** (tail) and journal message bodies (ruling 2, msg-1770).
- **User units** (``systemctl --user``): the probes run inside a system
  service, where the user manager's bus is not reachable as such.

Every value below was read off the running host (2026-11-25), not off a
document; the targets that are also deploy targets take their units and
health URL from :mod:`magickit.deploy.registry`, so the two tables cannot
disagree about them.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import MappingProxyType
from urllib.parse import urlparse

from magickit.deploy import registry as deploy_registry

#: The logical name of the services host -- ``{{HOST_SERVICES}}`` in
#: platform:infra-registry. Not the machine's hostname: probe output is
#: read by agents, and what an agent reads ends up in chatroom posts and
#: PR bodies, where real infra values must not appear.
HOST_SERVICES = "services"

#: magickit's own units. A probe of magickit is still "work on magickit"
#: (R-4), and for the later steps a restart of magickit would take down
#: the tool reporting the result (the same reason it does not deploy
#: itself, see :class:`magickit.deploy.registry.SelfDeployRefusedError`).
SELF_UNIT_PREFIX = "spirrow-magickit"

#: A unit name we are willing to put on an argv. No glob characters
#: (``journalctl -u`` and ``systemctl`` both expand them), no leading
#: ``-`` (would read as an option), system service units only.
_UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,99}\.service$")
#: A logical name (host, target, file).
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class HostRegistryError(Exception):
    """A name that is not in the registry, or a registry entry that is malformed."""


class UnknownHostError(HostRegistryError):
    """The host is not registered."""


class UnknownTargetError(HostRegistryError):
    """The target is not registered on that host."""


class UnknownFileError(HostRegistryError):
    """The file is not registered on that target."""


class SelfTargetRefusedError(HostRegistryError):
    """magickit's own units are not a target (R-4)."""


@dataclass(frozen=True)
class EnvFile:
    """A file in ``KEY=VALUE`` form that a target reads as data.

    Attributes:
        name: logical name callers use (never a path).
        path: absolute, normalised path with no symlink component. It is
            opened component by component with ``O_NOFOLLOW``
            (:mod:`magickit.hosts.fs`), so a symlink anywhere on it makes
            the read fail rather than follow -- registering the realpath
            is what makes that failure mean "something changed".
        consumers: who reads the file, for the record (msg-1757 M5). Step 2
            checks every consumer reads a value the same way before any
            key of the file becomes changeable.
    """

    name: str
    path: PurePosixPath
    consumers: tuple[str, ...] = ()


@dataclass(frozen=True)
class HostTarget:
    """One thing on a host that can be looked at.

    Attributes:
        name: logical name callers use.
        units: system units that make up the target.
        health_url: loopback URL whose status line says "it answers";
            ``None`` when the target has none.
        ports: TCP ports the target is expected to listen on.
        env_files: data files the target reads, by logical name.
        deploy_target: the :mod:`magickit.deploy.registry` name when the
            target is also deployable -- which is what gives it a
            ``deployed_sha``.
    """

    name: str
    units: tuple[str, ...]
    health_url: str | None
    ports: tuple[int, ...]
    env_files: Mapping[str, EnvFile] = field(default_factory=dict)
    deploy_target: str | None = None


@dataclass(frozen=True)
class Host:
    """A host and everything on it that may be looked at.

    Attributes:
        name: logical host name (see :data:`HOST_SERVICES`).
        kind: what the probes run against. ``linux-systemd`` only in v1;
            Windows hosts need different probes and a different sandbox
            (msg-1750 §2.5), so they are a different kind, not a flag.
        executor: how work reaches the host. ``local`` = this process
            runs the probe. A second host would be ``pull`` (it fetches
            work from magickit; no new inbound path), which v1 does not
            implement.
        targets: by name.
        disk_paths: filesystems the ``resources`` probe reports on.
    """

    name: str
    kind: str
    executor: str
    targets: Mapping[str, HostTarget]
    disk_paths: tuple[str, ...] = ("/",)


def _from_deploy(name: str, **extra: object) -> HostTarget:
    """A target that is also a deploy target: units and health come from there."""
    d = deploy_registry.resolve_target(name)
    port = urlparse(d.health_url).port if d.health_url else None
    return HostTarget(
        name=name,
        units=tuple(d.services),
        health_url=d.health_url,
        ports=(port,) if port else (),
        deploy_target=name,
        **extra,  # type: ignore[arg-type]
    )


#: ``as_posix`` because the suite also runs on the Windows loop host, where
#: ``SERVICES_ROOT`` is a ``WindowsPath``.
_RELEASES = PurePosixPath(deploy_registry.SERVICES_ROOT.as_posix()) / "releases"

_SERVICES_TARGETS: dict[str, HostTarget] = {
    "spirrow-conclair": _from_deploy(
        "spirrow-conclair",
        env_files=MappingProxyType(
            {
                # 0600. Read twice, by two parsers (measured 2026-11-25):
                # the unit's EnvironmentFile= and the app's pydantic-settings
                # env_file=".env" (cwd = the live slot, whose `.env` is a
                # relative symlink to this realpath). Step 2 has to show both
                # read a value the same way before any key here can change.
                "env": EnvFile(
                    name="env",
                    path=_RELEASES / "spirrow-conclair" / "shared" / ".env",
                    consumers=(
                        "systemd: EnvironmentFile= of spirrow-conclair.service",
                        "app: pydantic-settings env_file",
                    ),
                ),
            }
        ),
    ),
    # lexora's environment comes from an EnvironmentFile under /etc (root
    # owned) and from its executed start.sh: neither is a v1 file.
    "spirrow-lexora": _from_deploy("spirrow-lexora"),
    "spirrow-cognilens": _from_deploy("spirrow-cognilens"),
    "spirrow-prismind": _from_deploy("spirrow-prismind"),
    # Not deployable (no git working tree behind it), but the backend
    # lexora's light/medium/heavy tiers all land on, and so the most
    # common thing to look at. Probe-only: no env file, no deployed_sha.
    "vllm-32b": HostTarget(
        name="vllm-32b",
        units=("vllm-32b.service",),
        health_url="http://127.0.0.1:8000/health",
        ports=(8000,),
    ),
}

_HOSTS: dict[str, Host] = {
    HOST_SERVICES: Host(
        name=HOST_SERVICES,
        kind="linux-systemd",
        executor="local",
        targets=MappingProxyType(_SERVICES_TARGETS),
        disk_paths=("/",),
    ),
}


# -- validation ------------------------------------------------------------


def _check_unit(unit: str) -> None:
    if unit.startswith(SELF_UNIT_PREFIX):
        raise SelfTargetRefusedError(
            f"{unit!r} is magickit itself. Work on magickit is out of scope for "
            "host tasks in v1 (R-4)."
        )
    if not _UNIT_RE.match(unit):
        raise HostRegistryError(f"{unit!r} is not a plain system service unit name")


def _check_path(path: PurePosixPath) -> None:
    text = str(path)
    if not path.is_absolute() or posixpath.normpath(text) != text or "\x00" in text:
        raise HostRegistryError(f"{text!r} is not an absolute, normalised path")


def validate_host(host: Host) -> None:
    """Refuse a malformed host entry.

    Runs over the built-in table at import time (so a bad edit fails the
    test suite, not a probe) and over any table a test builds.

    Raises:
        HostRegistryError: on the first problem found.
    """
    if not _NAME_RE.match(host.name):
        raise HostRegistryError(f"host name {host.name!r} is not a logical name")
    if host.kind != "linux-systemd":
        raise HostRegistryError(f"host {host.name}: kind {host.kind!r} is not supported in v1")
    if host.executor != "local":
        raise HostRegistryError(
            f"host {host.name}: executor {host.executor!r} is not implemented in v1"
        )
    for name, target in host.targets.items():
        if name != target.name or not _NAME_RE.match(name):
            raise HostRegistryError(f"target {name!r} is mis-keyed or not a logical name")
        if not target.units:
            raise HostRegistryError(f"target {name}: no units")
        for unit in target.units:
            _check_unit(unit)
        for port in target.ports:
            if not 1 <= port <= 65535:
                raise HostRegistryError(f"target {name}: port {port} out of range")
        if target.health_url is not None:
            parsed = urlparse(target.health_url)
            if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
                raise HostRegistryError(
                    f"target {name}: health URL must be loopback http "
                    "(a probe must not become a way to reach other hosts)"
                )
        for fname, env in target.env_files.items():
            if fname != env.name or not _NAME_RE.match(fname):
                raise HostRegistryError(f"target {name}: file {fname!r} is mis-keyed")
            _check_path(env.path)
    for disk in host.disk_paths:
        _check_path(PurePosixPath(disk))


for _h in _HOSTS.values():
    validate_host(_h)


# -- lookup ----------------------------------------------------------------


def _looks_like_path(name: str) -> bool:
    return "/" in name or "\\" in name or name.startswith(".") or name != name.strip()


def host_names() -> tuple[str, ...]:
    """Registered hosts, sorted."""
    return tuple(sorted(_HOSTS))


def all_hosts() -> Mapping[str, Host]:
    """The registry table, read-only."""
    return MappingProxyType(_HOSTS)


def resolve_host(name: str, *, hosts: Mapping[str, Host] | None = None) -> Host:
    """Look a host up by logical name.

    Raises:
        UnknownHostError: for anything not registered, including a path or
            a real hostname.
    """
    table = _HOSTS if hosts is None else hosts
    if not isinstance(name, str) or _looks_like_path(name):
        raise UnknownHostError(
            f"{name!r} is not a host name. Registered: {', '.join(sorted(table))}"
        )
    try:
        return table[name]
    except KeyError:
        raise UnknownHostError(
            f"{name!r} is not a registered host. Registered: {', '.join(sorted(table))} "
            "(logical names; see platform:infra-registry)."
        ) from None


def resolve_target(host: Host, name: str) -> HostTarget:
    """Look a target up on a host.

    Raises:
        SelfTargetRefusedError: for magickit itself (R-4), before the table.
        UnknownTargetError: for anything else not registered.
    """
    if isinstance(name, str) and name.startswith(SELF_UNIT_PREFIX):
        raise SelfTargetRefusedError(
            "spirrow-magickit is not a host-task target: work on magickit itself is "
            "out of scope in v1 (R-4)."
        )
    if not isinstance(name, str) or _looks_like_path(name):
        raise UnknownTargetError(f"{name!r} is not a target name")
    try:
        return host.targets[name]
    except KeyError:
        raise UnknownTargetError(
            f"{name!r} is not a target on host {host.name}. Registered: "
            f"{', '.join(sorted(host.targets))}. Adding one is a pull request against "
            "magickit.hosts.registry."
        ) from None


def resolve_file(target: HostTarget, name: str) -> EnvFile:
    """Look a registered file up on a target.

    Raises:
        UnknownFileError: for anything not registered.
    """
    if not isinstance(name, str) or _looks_like_path(name):
        raise UnknownFileError(f"{name!r} is not a file name; files are named, not given by path")
    try:
        return target.env_files[name]
    except KeyError:
        registered = ", ".join(sorted(target.env_files)) or "none"
        raise UnknownFileError(
            f"{name!r} is not a registered file on {target.name}. Registered: {registered}."
        ) from None
