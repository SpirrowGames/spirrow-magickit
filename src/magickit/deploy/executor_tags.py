"""The one path that creates ``executor/vX.Y.Z`` tags (ADR-2026-10-09-23 D10).

D10 makes git's immutable tags the source of truth for the executor
version: semver, annotated, never moved -- a rollback is a *new* tag on an
older commit. The GitHub ruleset in ``config/github/executor-tags-ruleset.json``
forbids deleting or updating ``refs/tags/executor/*`` and restricts creation
to Magickit's identity; this module is the Magickit side of "creation only
through Magickit's deploy path".

What this module refuses, before anything is written to GitHub:

- ``invalid_version``: ``version`` is not exactly ``X.Y.Z`` (non-negative
  integers, no leading zeros, no ``v`` prefix, no pre-release or build
  suffix -- the tag form is ``executor/vX.Y.Z`` and nothing else).
- ``invalid_sha``: ``sha`` is not a full 40-hex commit id. A short sha is
  refused rather than resolved: what gets tagged must be exactly what the
  caller named.
- ``invalid_argument``: empty ``message`` or ``requested_by``.
- ``tag_exists``: the tag already exists. Nothing is re-pointed.
- ``sha_not_on_main``: the commit is unknown, or not reachable from the
  base branch (``main``).

Write order is annotated tag object first, then the ref. If the ref
creation loses a race (another caller created the same tag in between),
GitHub answers 422 and this module reports ``tag_exists``; the tag object
left behind is unreferenced and does not change any ref.

The GitHub transport is behind :class:`TagApi` so tests can substitute it.
"""

from __future__ import annotations

import re

# Patterns are applied with fullmatch: `$` would accept a trailing newline.
from dataclasses import dataclass
from typing import Any, Protocol

TAG_PREFIX = "executor/v"

_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

#: ``GET /compare/{sha}...{base}`` statuses meaning ``sha`` is an ancestor
#: of (or equal to) the base branch head.
_REACHABLE_STATUSES = frozenset({"ahead", "identical"})


class ExecutorTagError(Exception):
    """A refusal. ``error_type`` is the stable code returned to the caller."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: Any


class TagApi(Protocol):
    """The four GitHub REST calls this module needs."""

    async def get_ref(self, repo: str, ref: str) -> ApiResponse: ...

    async def compare(self, repo: str, base: str, head: str) -> ApiResponse: ...

    async def create_tag_object(
        self, repo: str, tag: str, message: str, sha: str
    ) -> ApiResponse: ...

    async def create_ref(self, repo: str, ref: str, sha: str) -> ApiResponse: ...


def tag_name(version: str) -> str:
    """``1.2.3`` -> ``executor/v1.2.3`` after strict validation."""
    if not isinstance(version, str) or not _SEMVER.fullmatch(version):
        raise ExecutorTagError(
            "invalid_version",
            f"version must be X.Y.Z (no 'v', no leading zeros, no suffix); got {version!r}",
        )
    return f"{TAG_PREFIX}{version}"


def validate_sha(sha: str) -> str:
    if not isinstance(sha, str) or not _FULL_SHA.fullmatch(sha):
        raise ExecutorTagError(
            "invalid_sha", f"sha must be a full 40-character lowercase hex commit id; got {sha!r}"
        )
    return sha


def _require_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutorTagError("invalid_argument", f"{name} must be a non-empty string")
    return value.strip()


def _tag_message(message: str, requested_by: str) -> str:
    return f"{message}\n\nRequested-by: {requested_by}\n"


async def create_executor_tag(
    api: TagApi,
    *,
    repo: str,
    version: str,
    sha: str,
    message: str,
    requested_by: str,
    base_branch: str = "main",
) -> dict[str, Any]:
    """Validate, then create the annotated tag object and its ref.

    Raises:
        ExecutorTagError: on any refusal (see module docstring). Nothing
            has been written to GitHub when a validation refusal is raised.
    """
    if not isinstance(repo, str) or not _REPO.fullmatch(repo):
        raise ExecutorTagError("invalid_argument", f"repo must be owner/name; got {repo!r}")
    name = tag_name(version)
    sha = validate_sha(sha)
    message = _require_text("message", message)
    requested_by = _require_text("requested_by", requested_by)

    ref = f"refs/tags/{name}"

    existing = await api.get_ref(repo, f"tags/{name}")
    if existing.status == 200:
        raise ExecutorTagError("tag_exists", f"{name} already exists in {repo}; tags are never moved")
    if existing.status != 404:
        raise ExecutorTagError(
            "github_error", f"could not check for {name} (HTTP {existing.status})"
        )

    cmp = await api.compare(repo, sha, base_branch)
    if cmp.status == 404:
        raise ExecutorTagError("sha_not_on_main", f"commit {sha} is unknown in {repo}")
    if cmp.status != 200:
        raise ExecutorTagError(
            "github_error", f"could not compare {sha} with {base_branch} (HTTP {cmp.status})"
        )
    status = cmp.body.get("status") if isinstance(cmp.body, dict) else None
    if status not in _REACHABLE_STATUSES:
        raise ExecutorTagError(
            "sha_not_on_main",
            f"commit {sha} is not reachable from {base_branch} (compare status {status!r})",
        )

    tag_obj = await api.create_tag_object(repo, name, _tag_message(message, requested_by), sha)
    if tag_obj.status != 201 or not isinstance(tag_obj.body, dict) or not tag_obj.body.get("sha"):
        raise ExecutorTagError(
            "github_error", f"could not create the tag object for {name} (HTTP {tag_obj.status})"
        )
    tag_sha = tag_obj.body["sha"]

    created = await api.create_ref(repo, ref, tag_sha)
    if created.status == 422:
        raise ExecutorTagError("tag_exists", f"{name} was created concurrently; not re-pointed")
    if created.status != 201:
        raise ExecutorTagError(
            "github_error", f"could not create {ref} (HTTP {created.status})"
        )

    return {
        "ok": True,
        "repo": repo,
        "tag": name,
        "ref": ref,
        "commit": sha,
        "tag_object": tag_sha,
        "requested_by": requested_by,
    }


class HttpxTagApi:
    """:class:`TagApi` over GitHub's REST API (``api.github.com``)."""

    def __init__(self, token: str, *, timeout: float = 30.0) -> None:
        self._token = token
        self._timeout = timeout

    async def _call(self, method: str, path: str, json: Any = None) -> ApiResponse:
        import httpx  # noqa: PLC0415 -- keep the import off module load

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.request(
                method, f"https://api.github.com/repos/{path}", headers=headers, json=json
            )
        try:
            body = resp.json()
        except ValueError:
            body = None
        return ApiResponse(status=resp.status_code, body=body)

    async def get_ref(self, repo: str, ref: str) -> ApiResponse:
        return await self._call("GET", f"{repo}/git/ref/{ref}")

    async def compare(self, repo: str, base: str, head: str) -> ApiResponse:
        return await self._call("GET", f"{repo}/compare/{base}...{head}")

    async def create_tag_object(
        self, repo: str, tag: str, message: str, sha: str
    ) -> ApiResponse:
        return await self._call(
            "POST",
            f"{repo}/git/tags",
            json={"tag": tag, "message": message, "object": sha, "type": "commit"},
        )

    async def create_ref(self, repo: str, ref: str, sha: str) -> ApiResponse:
        return await self._call("POST", f"{repo}/git/refs", json={"ref": ref, "sha": sha})
