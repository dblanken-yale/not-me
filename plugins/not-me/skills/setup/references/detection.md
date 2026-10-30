# Setup: what to detect before asking

Every probe here is a read. Run them all, then ask about what is left. Show the
user what was found next to each question so most answers are a confirmation.

| What | How to find it | Feeds |
|------|----------------|-------|
| Origin repo and default branch | `repo`, `tracker` and `default_branch` from the preflight output | `tracker`, `repos` |
| Operator login | `notme me` | roster first row (lead) |
| Repo labels | `gh label list --json name,description --limit 500` (read only) | `labels` mapping |
| Configured labels missing today | `notme labels-ensure --check` (`present`, `missing`) | labels step |
| Projects v2 boards | `gh project list --owner <owner> --format json` (read only); keep boards linked to the repo | `queue.source`, `queue.board` |
| Board columns | `gh project field-list <number> --owner <owner> --format json`; the single-select field named Status | `queue.board_states` |
| Collaborators | `gh api repos/<owner>/<name>/collaborators --jq '.[].login'` (a GET) | roster rows |
| Code owners | `CODEOWNERS`, `.github/CODEOWNERS`, `docs/CODEOWNERS` | roster rows, `reviewers` |
| Sibling checkouts | symlinks in the checkout, and sibling directories of the checkout that hold their own `.git`; read each one's origin | `repos` companion set |
| Test, lint, build commands | `package.json` scripts; `pyproject.toml` tool and script tables; the PHP package manifest scripts; `Makefile` targets named test, lint, check, build; `run:` steps in `.github/workflows/*.yml` | verify sections |
| Environment start | README sections on running locally, `docker-compose*.yml`, a Makefile target named up or start | `## Environment start` |
| Shared test environment | the environment start or test commands use one stack for the whole checkout (a local dev stack named by project, a fixed container or database, a fixed host port) rather than one per worktree; unsure: ask | `shared_env` in verify.md |
| Issue format | `.github/ISSUE_TEMPLATE/*`, then the ten most recent issues that have a goal and a checklist (`gh issue list --state all --limit 30 --json number,title,body`, read only) | `## Ticket format`, `## Issue readiness` |
| PR template | `.github/pull_request_template.md` | `pr_template` (leave `auto` when found) |
| Ranking signals (picking step) | `notme picking-signals` (a read): board fields and labels with counts over the eligible issues; no raw gh read; see `picking.md` | `picking.fields`, `## Picking work` |
| Existing not-me docs | the `layers` part of `notme config --explain` | which layer wins per doc |

## Rules

1. Read files with Read, Glob and Grep. Use Bash only for the reads listed above.
2. A probe that fails (no network, no scope, no board) is not an error. Note it and
   ask the question without a suggestion.
3. Prefer the command CI runs over a local alias when both exist. Record where each
   command came from so the verify section can say so.
4. Never copy secrets, tokens or URLs with credentials out of a workflow file.
5. A sibling checkout is a candidate only. It joins `repos` when the user says so.
