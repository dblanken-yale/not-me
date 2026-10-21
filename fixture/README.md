# Eval fixture

Contents and tooling for the two private throwaway GitHub repos the live evals run against.

| Path | Purpose |
|------|---------|
| `repo/` | Contents of the fixture repo's `main` (tiny `calc/` package, `site/`, workflows). Pushed once by `--bootstrap`. |
| `companion/` | Contents of the companion repo (`ui/` package). |
| `labels.json` | The eleven labels both repos carry. |
| `scenarios/<ID>.json` | What `--scenario ID` creates, and what the run is expected to do (`expect`). |
| `seed_fixture.py` | Bootstrap, reset, seed, teardown and verify. Stdlib only, calls `gh` and `git`. |
| `tests/` | Unit tests for the seeder, run by `scripts/run-tests.py` with a fake `run`. |

Note: `repo/_github` and `companion/_github` are stored under that name so GitHub does not
run their workflows from the plugin repo. `--bootstrap` renames `_github` to `.github` when it
stages the temporary clone.

## One-time setup

1. Create two private repos, for example `OWNER/not-me-fixture` and `OWNER/not-me-fixture-companion`.
   Names must end in `-fixture` and `-fixture-companion`.
2. In the fixture repo: Settings, Actions, General, Workflow permissions, tick "Allow GitHub Actions
   to create and approve pull requests". The `post-as-bot.yml` kinds `pr` and `review` need it.
3. Authenticate `gh` with a token that can push workflow files, delete issues and branches in those
   two repos, and dispatch workflows (`repo` and `workflow` scopes, or an equivalent fine-grained token).
4. Bootstrap, which pushes `repo/` as the first commit, tags it `fixture-baseline`, creates `develop`
   and syncs labels:

       python3 fixture/seed_fixture.py --owner OWNER --repo OWNER/not-me-fixture \
           --companion OWNER/not-me-fixture-companion --bootstrap

Flags can come from `NOT_ME_FIXTURE_OWNER`, `NOT_ME_FIXTURE_REPO` and `NOT_ME_FIXTURE_COMPANION`.

## Per run

    seed_fixture.py ... --reset --run-id RID      # known state, keeps the lock
    seed_fixture.py ... --scenario N1 --run-id RID  # seeds, writes evals/.state/RID/N1.json
    seed_fixture.py ... --verify --manifest evals/.state/RID/N1.json   # nothing changed since seeding?
    seed_fixture.py ... --teardown RID            # clean up and release the lock

Add `--dry-run` to any of them to print the commands without running anything.

## Safety

The seeder force-moves refs and deletes branches and issues, so it refuses unless the repo name ends
in `-fixture` or `-fixture-companion`, the owner equals `--owner`, and the repo is private. Only one run
holds the `eval-lock` issue at a time (stale after two hours). Every write is logged to stderr and
stored in the manifest.

## Scenario format

    {"patches": [{"path", "find", "replace", "branch" | "on_main": true, "base", "message"}],
     "issues":  [{"key", "title", "body", "labels", "assignee", "as": "operator|bot", "comments"}],
     "prs":     [{"key", "head", "base", "title", "body", "labels", "assignee", "as", "reviewers",
                  "review_threads": [{"path", "line", "body", "as"}],
                  "comments": [{"body", "as"}], "reviews": [{"state", "body", "as"}]}],
     "companion": {same shape, operator only},
     "expect": {free-form, copied into the manifest}}

`{rid}`, `{id}` and `{operator}` are replaced in every string except `find` and `replace`. Titles get the
prefix `[eval:RID:ID]`. `"as": "bot"` dispatches `post-as-bot.yml` and waits for it. Patches are committed
through the contents API (no clone); `review` state is `comment`, `approve` or `request_changes`.
Bot-created PRs do not trigger CI.
