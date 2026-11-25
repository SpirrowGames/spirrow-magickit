"""Validators rebuild values; the scanner is a last check on what survives."""

from __future__ import annotations

from collections import Counter

import pytest

from magickit.hosts import fields, secretscan


@pytest.mark.parametrize(
    "value,expected",
    [
        ("12", 12),
        (12, 12),
        ("0", 0),
        ("-1", None),
        (" 12", None),
        ("12 ", None),
        ("0x10", None),
        ("1e3", None),
        ("12.0", None),
        (True, None),
        (None, None),
        (["1"], None),
        ("99", None),  # out of range below
        ("", None),
    ],
)
def test_int_in(value, expected):
    assert fields.int_in(value, 0, 50) == expected


def test_hex32_only_lowercase_32_hex():
    assert fields.hex32("39f53479d3a045ac8e11786248231fbf") == "39f53479d3a045ac8e11786248231fbf"
    for bad in [
        "39F53479D3A045AC8E11786248231FBF",
        "39f53479d3a045ac8e11786248231fb",
        "39f53479-d3a0-45ac-8e11-786248231fbf",
        "token=hunter2hunter2hunter2hunter2",
        ["39f53479d3a045ac8e11786248231fbf"],
    ]:
        assert fields.hex32(bad) is None


def test_slug_is_short_and_lowercase():
    assert fields.slug("auto-restart") == "auto-restart"
    for bad in ["Running", "running now", "x" * 33, "1abc", "a_b", ""]:
        assert fields.slug(bad) is None


def test_env_key():
    assert fields.env_key("DATABASE_URL") == "DATABASE_URL"
    for bad in ["1ABC", "A-B", "A B", "", "A" * 129, "A=B"]:
        assert fields.env_key(bad) is None


@pytest.mark.parametrize(
    "text,rule",
    [
        ("ghp_" + "a" * 36, "key"),
        ("github_pat_" + "A1b2" * 10, "key"),
        ("sk-ant-" + "x" * 30, "key"),
        ("Authorization: Basic Zm9vOmJhcg==", "auth_header"),
        ("Bearer abcdefgh12345", "bearer"),
        ("postgresql://app:hunter2@db/x", "url_userinfo"),
        ("password=hunter2", "keyword_kv"),
        ("-----BEGIN PRIVATE KEY-----", "pem"),
        ("Xk9vQ2pLm3Rt8Wz1Yb5Nc7Hd4Jf6Gs0A", "entropy"),
    ],
)
def test_secret_like(text, rule):
    assert secretscan.secret_like(text) == rule


@pytest.mark.parametrize(
    "text",
    [
        "spirrow-lexora.service",
        "2026-11-25T00:31:04+00:00",
        "39f53479d3a045ac8e11786248231fbf",  # MESSAGE_ID: hex is excluded
        "d48ebad77d9dab48c27dc7ca2b334e938811e4d1",  # git sha
        "NVIDIA GeForce RTX 5090",
        "DATABASE_URL",
        "SANCTION_RECORDING_SINCE",
        "auto-restart",
    ],
)
def test_ordinary_probe_values_are_not_secret_like(text):
    assert secretscan.secret_like(text) is None


def test_scrub_drops_and_counts_but_never_returns_the_value():
    secret = "ghp_" + "Q" * 36
    counts: Counter[str] = Counter()
    out = secretscan.scrub({"keys": ["OK", secret], secret: 1, "n": 3}, counts)
    assert out == {"keys": ["OK", None], "n": 3}
    assert counts == Counter({"secret_like:key": 2})
    assert secret not in repr(out) and secret not in repr(counts)
