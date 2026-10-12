# GitHub rulesets for `executor/*` tags (ADR-2026-10-09-23 D10)

These files are **not applied by Magickit**. Applying them is a manual
operator task after the PR that adds them is merged. The target repository is
`settings.executor_tag_repo` (default `SpirrowGames/spirrow-mindwire`).

Two rulesets, because GitHub applies `bypass_actors` to a whole ruleset:

| file | rules | bypass |
|---|---|---|
| `executor-tags-immutable.ruleset.json` | deletion, update | **nobody** |
| `executor-tags-create.ruleset.json` | creation | Magickit's identity only |

`non_fast_forward` is not included. It is a branch rule; on a tag, moving the
ref (forced or not) is an `update`, which this file already forbids with no
bypass, so it adds no protection, and the PR-gate review of #106 reported that
the API refuses it on `target: tag`, which would leave the whole file
unapplied.

One file with all the rules would let the bypass actor also move or delete a
tag, which D10 forbids for everyone.

## Before applying `executor-tags-create.ruleset.json`

`actor_id` is deliberately `null`, so the API refuses this file until somebody
fills it in. Ruleset bypass actors cannot be individual users (the allowed
`actor_type` values are `Integration`, `OrganizationAdmin`, `RepositoryRole`,
`Team`, `DeployKey`), so "Magickit's identity" has to be a GitHub App
(`Integration`) or a team that holds only that identity.

Today Magickit calls GitHub with `GITHUB_MCP_PAT_IMPLEMENTER`, and that
account is also the one the autonomous loop's implementer uses with `gh`.
Bypassing that account would let the loop create `executor/*` tags directly,
outside `executor_tag_create`. Choose the bypass actor so that it is not
shared with the loop, and set `MAGICKIT_EXECUTOR_TAG_PAT_ENV` to the name of
the environment variable that holds that identity's token (setting
`executor_tag_pat_env`; default `GITHUB_MCP_PAT_IMPLEMENTER`).

Apply with an admin token:

```
gh api -X POST repos/<owner>/<repo>/rulesets --input executor-tags-immutable.ruleset.json
gh api -X POST repos/<owner>/<repo>/rulesets --input executor-tags-create.ruleset.json
```
