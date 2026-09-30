"""PR 一覧 (``/dashboard/prs``).

What these pin, in order of how badly the page would lie without them:

- a failed search renders as "could not read", never as "0 件";
- "gate 済" is exactly the PRs ``pr_watch`` calls approved — the page
  does not have its own idea of it;
- a PR listed without a gate verdict (release / 依頼) says so on its row,
  and is never listed while it is a draft or conflicting;
- 停滞 is "no update for N hours", drafts included;
- excluded repos are left out of the search *and* named on the page.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from magickit.config import Settings
from magickit.core.pr_watch import PrSnapshot, PrWatchState
from magickit.web import prs

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _item(
    repo: str,
    number: int,
    *,
    updated: datetime = NOW,
    created: datetime | None = None,
    draft: bool = False,
    owner: str = "SpirrowGames",
    assignees: tuple[str, ...] = (),
) -> dict[str, Any]:
    item = _bare_item(repo, number, updated=updated, created=created, draft=draft, owner=owner)
    if assignees:
        # GitHub omits nothing here, but github-mcp serialises with
        # omitempty: the key is absent when nobody is assigned.
        item["assignees"] = [{"login": login} for login in assignees]
    return item


def _bare_item(
    repo: str,
    number: int,
    *,
    updated: datetime,
    created: datetime | None,
    draft: bool,
    owner: str,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"{repo} change {number}",
        "html_url": f"https://github.com/{owner}/{repo}/pull/{number}",
        "repository_url": f"https://api.github.com/repos/{owner}/{repo}",
        "user": {"login": "takahito-spirrowgames"},
        "created_at": _iso(created or updated),
        "updated_at": _iso(updated),
        "draft": draft,
    }


def _search_returning(*pages: list[dict[str, Any]], total: int | None = None):
    calls: list[tuple[str, int]] = []
    count = total if total is not None else sum(len(p) for p in pages)

    async def search_page(query: str, page: int) -> Any:
        calls.append((query, page))
        items = pages[page - 1] if page - 1 < len(pages) else []
        return {"total_count": count, "incomplete_results": False, "items": items}

    search_page.calls = calls  # type: ignore[attr-defined]
    return search_page


def _snapshot(
    repo: str,
    number: int,
    *,
    approved: bool,
    rate_capped: bool = False,
    head_ref: str = "",
    base_ref: str = "",
    head_in_base_repo: bool = False,
) -> PrSnapshot:
    return PrSnapshot(
        owner="SpirrowGames",
        repo=repo,
        number=number,
        title="t",
        html_url="u",
        head_sha="abc",
        updated_at=_iso(NOW),
        artifact_approved=approved,
        rate_capped=rate_capped,
        head_ref=head_ref,
        base_ref=base_ref,
        head_in_base_repo=head_in_base_repo,
    )


def _settings(**overrides: Any) -> Settings:
    base = {
        "prs_org": "SpirrowGames",
        "prs_exclude_repos": ["thirdy-sandbox"],
        "prs_stale_hours": 24,
    }
    base.update(overrides)
    return Settings(**base)


# --- query / exclusion -----------------------------------------------------


def test_query_excludes_each_repo_inside_the_org():
    assert prs.build_query("SpirrowGames", ["thirdy-sandbox", "old"]) == (
        "org:SpirrowGames is:pr is:open "
        "-repo:SpirrowGames/thirdy-sandbox -repo:SpirrowGames/old"
    )


def test_exclude_entries_normalise_to_bare_names():
    names = prs.excluded_repo_names(
        "SpirrowGames",
        ["thirdy-sandbox", "SpirrowGames/old", "spirrowgames/old", "Other/x", "", None],
    )
    # owner/repo inside the org is the same repo; another owner cannot be
    # excluded from an org: search, so it is dropped rather than emitted as
    # a qualifier that does nothing.
    assert names == ["thirdy-sandbox", "old"]


def test_yaml_prs_section_is_read(tmp_path):
    import yaml

    path = tmp_path / "magickit_config.yaml"
    path.write_text(
        yaml.safe_dump(
            {"prs": {"org": "Acme", "stale_hours": 48, "exclude_repos": [], "refresh_seconds": 60}}
        ),
        encoding="utf-8",
    )
    settings = Settings.from_yaml(path)
    assert settings.prs_org == "Acme"
    assert settings.prs_stale_hours == 48
    # An explicit empty list means "hide nothing", not "use the default".
    assert settings.prs_exclude_repos == []
    assert settings.prs_refresh_seconds == 60


def test_yaml_merge_signals_are_read_and_empty_turns_them_off(tmp_path):
    import yaml

    path = tmp_path / "magickit_config.yaml"
    path.write_text(
        yaml.safe_dump({"prs": {"release_heads": [], "merge_assignees": ["takayan0908"]}}),
        encoding="utf-8",
    )
    settings = Settings.from_yaml(path)
    assert settings.prs_release_heads == []
    assert settings.prs_merge_assignees == ["takayan0908"]


def test_merge_signal_defaults():
    # Nobody's login is baked into the code; `develop` is a convention.
    settings = Settings()
    assert settings.prs_release_heads == ["develop"]
    assert settings.prs_merge_assignees == []


def test_yaml_partial_prs_section_keeps_defaults(tmp_path):
    # Absent keys reach `flat_config` as None; `from_yaml` strips None
    # before building Settings, so they fall back to the field defaults
    # instead of failing validation.
    import yaml

    path = tmp_path / "magickit_config.yaml"
    path.write_text(yaml.safe_dump({"prs": {"stale_hours": 48}}), encoding="utf-8")
    settings = Settings.from_yaml(path)
    assert settings.prs_stale_hours == 48
    assert settings.prs_org == "SpirrowGames"
    assert settings.prs_refresh_seconds == 300
    assert settings.prs_exclude_repos == ["thirdy-sandbox"]
    assert settings.prs_release_heads == ["develop"]


# --- search ----------------------------------------------------------------


async def test_search_rows_carry_repo_from_repository_url():
    rows, notices = await prs.search_open_prs(
        "q", search_page=_search_returning([_item("spirrow-mindwire", 343, draft=True)])
    )
    assert notices == []
    assert rows is not None and len(rows) == 1
    row = rows[0]
    assert (row.owner, row.repo, row.number) == ("SpirrowGames", "spirrow-mindwire", 343)
    assert row.html_url == "https://github.com/SpirrowGames/spirrow-mindwire/pull/343"
    assert row.author == "takahito-spirrowgames"
    assert row.draft is True
    assert row.assignees == ()


async def test_search_rows_carry_assignees():
    rows, _ = await prs.search_open_prs(
        "q",
        search_page=_search_returning(
            [_item("spirrow-docs", 44, assignees=("takayan0908", "dany1468"))]
        ),
    )
    assert rows is not None
    assert rows[0].assignees == ("takayan0908", "dany1468")


async def test_search_error_envelope_is_could_not_read_not_empty():
    async def search_page(query: str, page: int) -> Any:
        return {"message": "API rate limit exceeded"}

    rows, notices = await prs.search_open_prs("q", search_page=search_page)
    assert rows is None
    assert any("API rate limit exceeded" in n for n in notices)


async def test_search_exception_is_could_not_read():
    async def search_page(query: str, page: int) -> Any:
        raise RuntimeError("connect timeout")

    rows, notices = await prs.search_open_prs("q", search_page=search_page)
    assert rows is None
    assert any("connect timeout" in n for n in notices)


async def test_search_paginates_and_reports_what_it_could_not_fetch():
    full = [_item("r", n) for n in range(100)]
    search_page = _search_returning(full, full, full, total=450)
    rows, notices = await prs.search_open_prs("q", search_page=search_page)
    assert rows is not None and len(rows) == 300
    assert [p for _, p in search_page.calls] == [1, 2, 3]
    assert any("450" in n and "300" in n for n in notices)


async def test_search_stops_on_a_short_page():
    search_page = _search_returning([_item("r", 1), _item("r", 2)])
    rows, _ = await prs.search_open_prs("q", search_page=search_page)
    assert rows is not None and len(rows) == 2
    assert len(search_page.calls) == 1


# --- read_overview ---------------------------------------------------------


async def test_merge_ready_is_exactly_what_pr_watch_approved():
    seen_repos: list[list[tuple[str, str]]] = []

    async def collect(repos, state):
        seen_repos.append(repos)
        return (
            [
                _snapshot("spirrow-mindwire", 348, approved=True),
                _snapshot("spirrow-mindwire", 353, approved=False),
            ],
            [],
            set(),
        )

    search = _search_returning(
        [
            _item("spirrow-mindwire", 348),
            _item("spirrow-mindwire", 353),
            _item("spirrow-playproof", 99, draft=True),
        ]
    )
    overview = await prs.read_overview(
        _settings(), search_page=search, collect_snapshots=collect, state=PrWatchState()
    )
    assert overview.approved == {("SpirrowGames", "spirrow-mindwire", 348)}
    # A repo whose only open PR is a draft is not asked about reviews.
    assert seen_repos == [[("SpirrowGames", "spirrow-mindwire")]]
    assert "-repo:SpirrowGames/thirdy-sandbox" in search.calls[0][0]


async def test_release_is_a_release_head_in_the_base_repository():
    async def collect(repos, state):
        return (
            [
                # The release: develop -> main, same repo, no gate verdict.
                _snapshot("p", 113, approved=False, head_ref="develop",
                          base_ref="main", head_in_base_repo=True),
                # A fork's branch that happens to be called develop.
                _snapshot("p", 114, approved=False, head_ref="develop",
                          base_ref="main", head_in_base_repo=False),
                # An ordinary feature PR into develop.
                _snapshot("p", 115, approved=False, head_ref="feature/x",
                          base_ref="develop", head_in_base_repo=True),
                # Branch names are exact: `develop-2` is not `develop`.
                _snapshot("p", 116, approved=False, head_ref="develop-2",
                          base_ref="main", head_in_base_repo=True),
            ],
            [],
            set(),
        )

    overview = await prs.read_overview(
        _settings(),
        search_page=_search_returning([_item("p", n) for n in (113, 114, 115, 116)]),
        collect_snapshots=collect,
        state=PrWatchState(),
    )
    assert overview.release == {("SpirrowGames", "p", 113): "develop → main"}
    assert overview.approved == set()
    assert overview.mergeable == {("SpirrowGames", "p", n) for n in (113, 114, 115, 116)}


async def test_release_heads_follow_setting():
    async def collect(repos, state):
        return (
            [
                _snapshot("p", 1, approved=False, head_ref="develop",
                          base_ref="main", head_in_base_repo=True),
                _snapshot("p", 2, approved=False, head_ref="staging",
                          base_ref="main", head_in_base_repo=True),
            ],
            [],
            set(),
        )

    overview = await prs.read_overview(
        _settings(prs_release_heads=["staging"]),
        search_page=_search_returning([_item("p", 1), _item("p", 2)]),
        collect_snapshots=collect,
        state=PrWatchState(),
    )
    assert overview.release == {("SpirrowGames", "p", 2): "staging → main"}


async def test_gate_failure_keeps_stale_and_says_merge_is_unknown():
    async def collect(repos, state):
        raise RuntimeError("github-mcp down")

    overview = await prs.read_overview(
        _settings(),
        search_page=_search_returning([_item("r", 1, updated=NOW - timedelta(days=3))]),
        collect_snapshots=collect,
        state=PrWatchState(),
    )
    assert overview.approved == set()
    assert overview.mergeable == set() and overview.release == {}
    assert len(overview.rows) == 1
    assert any("不明" in n for n in overview.notices)


async def test_rate_capped_unapproved_prs_are_named_as_possibly_missing():
    async def collect(repos, state):
        return ([_snapshot("r", 1, approved=False, rate_capped=True)], [], set())

    overview = await prs.read_overview(
        _settings(),
        search_page=_search_returning([_item("r", 1)]),
        collect_snapshots=collect,
        state=PrWatchState(),
    )
    assert any("rate-cap" in n for n in overview.notices)


async def test_search_failure_raises_rather_than_returning_empty():
    async def search_page(query: str, page: int) -> Any:
        return None

    with pytest.raises(prs._SearchFailedError):
        await prs.read_overview(_settings(), search_page=search_page, state=PrWatchState())


# --- build_context ---------------------------------------------------------


def _overview(
    rows_items: list[dict[str, Any]], approved=(), mergeable=(), release=None
) -> prs.Overview:
    rows = [prs._row_from_item(i) for i in rows_items]
    return prs.Overview(
        checked_at=NOW,
        rows=[r for r in rows if r],
        approved=set(approved),
        notices=[],
        mergeable=set(mergeable) | set(approved),
        release=dict(release or {}),
    )


def _key(repo: str, number: int) -> tuple[str, str, int]:
    return ("SpirrowGames", repo, number)


def test_stale_is_no_update_for_n_hours_drafts_included_oldest_first():
    overview = _overview(
        [
            _item("a", 1, updated=NOW - timedelta(hours=23, minutes=59)),  # fresh
            _item("a", 2, updated=NOW - timedelta(hours=24)),  # boundary: stale
            _item("a", 3, updated=NOW - timedelta(days=17), draft=True),
            # Opened long ago but touched today: not stale.
            _item("a", 4, updated=NOW - timedelta(hours=1), created=NOW - timedelta(days=9)),
        ]
    )
    ctx = prs.build_context(overview, [], _settings(), now=NOW)
    assert [r.number for r in ctx["stale"]] == [3, 2]


def test_stale_threshold_follows_setting():
    overview = _overview([_item("a", 1, updated=NOW - timedelta(hours=30))])
    assert prs.build_context(overview, [], _settings(prs_stale_hours=48), now=NOW)["stale"] == []


def test_merge_ready_oldest_waiting_first():
    overview = _overview(
        [
            _item("a", 1, updated=NOW - timedelta(hours=1)),
            _item("a", 2, updated=NOW - timedelta(days=2)),
            _item("a", 3, updated=NOW - timedelta(days=5)),
        ],
        approved=[("SpirrowGames", "a", 1), ("SpirrowGames", "a", 2)],
    )
    ctx = prs.build_context(overview, [], _settings(), now=NOW)
    assert [r.number for r in ctx["merge_ready"]] == [2, 1]


def test_merge_ready_lists_release_and_assigned_prs_with_their_reason():
    overview = _overview(
        [
            _item("a", 1),  # gate 済
            _item("p", 113),  # release, no gate verdict
            _item("docs", 44, assignees=("Takayan0908",)),  # asked, no gate
            _item("a", 5),  # plain open PR: nobody is waiting on a merge
        ],
        approved=[_key("a", 1)],
        mergeable=[_key("p", 113), _key("docs", 44), _key("a", 5)],
        release={_key("p", 113): "develop → main"},
    )
    ctx = prs.build_context(
        overview, [], _settings(prs_merge_assignees=["takayan0908"]), now=NOW
    )
    assert sorted(r.number for r in ctx["merge_ready"]) == [1, 44, 113]
    labels = {key: [r.label for r in rs] for key, rs in ctx["merge_reasons"].items()}
    assert labels == {
        _key("a", 1): ["gate 済"],
        _key("p", 113): ["release develop → main"],
        # Logins compare case-insensitively; the row shows GitHub's spelling.
        _key("docs", 44): ["依頼 → Takayan0908"],
    }


def test_a_row_can_carry_every_reason_at_once():
    overview = _overview(
        [_item("p", 113, assignees=("takayan0908",))],
        approved=[_key("p", 113)],
        release={_key("p", 113): "develop → main"},
    )
    ctx = prs.build_context(
        overview, [], _settings(prs_merge_assignees=["takayan0908"]), now=NOW
    )
    assert [r.kind for r in ctx["merge_reasons"][_key("p", 113)]] == ["gate", "release", "asked"]
    assert len(ctx["merge_ready"]) == 1


def test_assignee_does_not_list_a_pr_that_cannot_be_merged():
    # Not in `mergeable` = pr_watch dropped it (draft / conflicting) or
    # could not read its repo. "Click merge" would be a lie either way.
    overview = _overview(
        [
            _item("docs", 44, assignees=("takayan0908",), draft=True),
            _item("docs", 45, assignees=("takayan0908",)),
        ]
    )
    ctx = prs.build_context(
        overview, [], _settings(prs_merge_assignees=["takayan0908"]), now=NOW
    )
    assert ctx["merge_ready"] == []


def test_assignee_outside_the_setting_is_not_a_merge_request():
    # dany1468 assigns their own PRs to themselves (UeRestartCommand#1).
    overview = _overview(
        [_item("UeRestartCommand", 1, assignees=("dany1468",))],
        mergeable=[_key("UeRestartCommand", 1)],
    )
    asked = prs.build_context(
        overview, [], _settings(prs_merge_assignees=["takayan0908"]), now=NOW
    )
    assert asked["merge_ready"] == []
    # And with the setting empty (the code default) nobody's assignment counts.
    assert prs.build_context(overview, [], _settings(), now=NOW)["merge_ready"] == []


# --- cache -----------------------------------------------------------------


async def test_overview_is_cached_and_failures_are_not(monkeypatch):
    monkeypatch.setattr(prs, "_CACHE", None)
    calls = {"n": 0, "fail": True}

    async def fake_read(settings):
        calls["n"] += 1
        if calls["fail"]:
            raise prs._SearchFailedError(["down"])
        return _overview([])

    monkeypatch.setattr(prs, "read_overview", fake_read)
    settings = _settings(prs_refresh_seconds=300)

    overview, notices = await prs.get_overview(settings)
    assert overview is None and notices == ["down"]
    calls["fail"] = False
    first, _ = await prs.get_overview(settings)  # failure was not cached
    second, _ = await prs.get_overview(settings)  # success is
    assert first is second
    assert calls["n"] == 2


# --- routes ----------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(prs, "get_settings", lambda: _settings())
    app = FastAPI()
    app.include_router(prs.router)
    return TestClient(app)


def test_fragment_links_every_row_to_github(client, monkeypatch):
    overview = _overview(
        [
            _item("spirrow-mindwire", 348),
            _item("UeRestartCommand", 1, updated=NOW - timedelta(days=17)),
        ],
        approved=[("SpirrowGames", "spirrow-mindwire", 348)],
    )

    async def fake_get(settings):
        return overview, []

    monkeypatch.setattr(prs, "get_overview", fake_get)
    html = client.get("/dashboard/prs/_tables").text
    assert 'href="https://github.com/SpirrowGames/spirrow-mindwire/pull/348"' in html
    assert 'href="https://github.com/SpirrowGames/UeRestartCommand/pull/1"' in html
    assert "thirdy-sandbox" in html  # excluded repos are named, not hidden


def test_fragment_names_the_reason_on_each_merge_row(client, monkeypatch):
    overview = _overview(
        [_item("spirrow-mindwire", 348), _item("spirrow-playproof", 113)],
        approved=[_key("spirrow-mindwire", 348)],
        mergeable=[_key("spirrow-playproof", 113)],
        release={_key("spirrow-playproof", 113): "develop → main"},
    )

    async def fake_get(settings):
        return overview, []

    monkeypatch.setattr(prs, "get_overview", fake_get)
    html = client.get("/dashboard/prs/_tables").text
    assert "マージ待ち" in html and "2 件" in html
    assert ">gate 済</span>" in html
    assert ">release develop → main</span>" in html


def test_fragment_on_search_failure_says_unknown_not_none(client, monkeypatch):
    async def fake_get(settings):
        return None, ["GitHub の PR 検索に失敗しました (boom)"]

    monkeypatch.setattr(prs, "get_overview", fake_get)
    html = client.get("/dashboard/prs/_tables").text
    assert "読めませんでした" in html
    assert "boom" in html
    assert ">なし<" not in html


def test_page_shell_polls_the_fragment(client):
    html = client.get("/dashboard/prs").text
    assert 'hx-get="/dashboard/prs/_tables"' in html
