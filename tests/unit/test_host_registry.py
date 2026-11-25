"""The host registry: two levels, names only, magickit refused (R-4)."""

from __future__ import annotations

from pathlib import PurePosixPath
from types import MappingProxyType

import pytest

from magickit.deploy import registry as deploy_registry
from magickit.hosts import registry
from magickit.hosts.registry import (
    EnvFile,
    Host,
    HostRegistryError,
    HostTarget,
    SelfTargetRefusedError,
    UnknownFileError,
    UnknownHostError,
    UnknownTargetError,
)


def test_two_levels_host_then_targets():
    host = registry.resolve_host(registry.HOST_SERVICES)
    assert host.kind == "linux-systemd"
    assert host.executor == "local"
    assert set(host.targets) == {
        "spirrow-conclair",
        "spirrow-lexora",
        "spirrow-cognilens",
        "spirrow-prismind",
        "vllm-32b",
    }


def test_host_key_is_logical_not_a_hostname():
    # Probe output lands in agent context; the registry must not make the
    # real hostname part of every call.
    for name in registry.host_names():
        assert not name.startswith("sg-")
    with pytest.raises(UnknownHostError):
        registry.resolve_host("sg-example-01")


@pytest.mark.parametrize(
    "name", ["../services", "/services", " services", "services/", ".services"]
)
def test_path_shaped_host_names_are_refused(name):
    with pytest.raises(UnknownHostError):
        registry.resolve_host(name)


def test_deploy_targets_take_units_and_health_from_the_deploy_registry():
    host = registry.resolve_host(registry.HOST_SERVICES)
    for name in deploy_registry.target_names():
        t = host.targets[name]
        d = deploy_registry.resolve_target(name)
        assert t.units == tuple(d.services)
        assert t.health_url == d.health_url
        assert t.deploy_target == name


@pytest.mark.parametrize(
    "name", ["spirrow-magickit", "spirrow-magickit-mcp", "spirrow-magickit-mcp-local"]
)
def test_magickit_is_refused_before_the_table(name):
    host = registry.resolve_host(registry.HOST_SERVICES)
    with pytest.raises(SelfTargetRefusedError):
        registry.resolve_target(host, name)


def test_magickit_units_cannot_be_registered():
    bad = Host(
        name="h",
        kind="linux-systemd",
        executor="local",
        targets={
            "t": HostTarget(
                name="t", units=("spirrow-magickit-mcp-local.service",), health_url=None, ports=()
            )
        },
    )
    with pytest.raises(SelfTargetRefusedError):
        registry.validate_host(bad)


@pytest.mark.parametrize(
    "unit",
    ["spirrow-*.service", "-H.service", "a b.service", "x.socket", "x.service;id", "[ab].service"],
)
def test_unit_names_with_globs_options_or_other_types_are_refused(unit):
    bad = Host(
        name="h",
        kind="linux-systemd",
        executor="local",
        targets={"t": HostTarget(name="t", units=(unit,), health_url=None, ports=())},
    )
    with pytest.raises(HostRegistryError):
        registry.validate_host(bad)


@pytest.mark.parametrize(
    "url", ["http://10.0.0.1:80/health", "https://127.0.0.1/health", "file:///etc/passwd"]
)
def test_health_urls_must_be_loopback_http(url):
    bad = Host(
        name="h",
        kind="linux-systemd",
        executor="local",
        targets={"t": HostTarget(name="t", units=("x.service",), health_url=url, ports=())},
    )
    with pytest.raises(HostRegistryError):
        registry.validate_host(bad)


@pytest.mark.parametrize("path", ["relative/.env", "/a/../b/.env"])
def test_env_file_paths_must_be_absolute_and_normalised(path):
    bad = Host(
        name="h",
        kind="linux-systemd",
        executor="local",
        targets={
            "t": HostTarget(
                name="t",
                units=("x.service",),
                health_url=None,
                ports=(),
                env_files=MappingProxyType({"env": EnvFile(name="env", path=PurePosixPath(path))}),
            )
        },
    )
    with pytest.raises(HostRegistryError):
        registry.validate_host(bad)


def test_only_v1_host_kinds_and_executors():
    t = {"t": HostTarget(name="t", units=("x.service",), health_url=None, ports=())}
    with pytest.raises(HostRegistryError):
        registry.validate_host(Host(name="h", kind="windows", executor="local", targets=t))
    with pytest.raises(HostRegistryError):
        registry.validate_host(Host(name="h", kind="linux-systemd", executor="pull", targets=t))


def test_files_are_named_not_given_by_path():
    host = registry.resolve_host(registry.HOST_SERVICES)
    conclair = registry.resolve_target(host, "spirrow-conclair")
    assert registry.resolve_file(conclair, "env").name == "env"
    for bad in ["../.env", "/etc/passwd", ".env", "start.sh"]:
        with pytest.raises(UnknownFileError):
            registry.resolve_file(conclair, bad)


def test_no_executed_file_is_registered():
    # msg-1757 A-1: a change to an executed file is a change to code.
    for host in registry.all_hosts().values():
        for target in host.targets.values():
            for env in target.env_files.values():
                assert not str(env.path).endswith((".sh", ".service", ".py"))
                assert "start" not in env.path.name


def test_registered_paths_avoid_the_release_symlink():
    # msg-1757 B-6: register the realpath; "current" / the stable service
    # symlink would let a release switch change what is read.
    for host in registry.all_hosts().values():
        for target in host.targets.values():
            for env in target.env_files.values():
                assert "current" not in env.path.parts
                assert "releases" in env.path.parts and "shared" in env.path.parts


def test_unknown_target_names_the_registered_ones():
    host = registry.resolve_host(registry.HOST_SERVICES)
    with pytest.raises(UnknownTargetError) as err:
        registry.resolve_target(host, "rag-server")
    assert "spirrow-lexora" in str(err.value)
