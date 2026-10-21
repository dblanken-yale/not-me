# Testing

Unit tests and lint need nothing but Python: `python3 scripts/run-tests.py` and `python3 scripts/lint-evals.py`.
This page covers the live evals, which run skills against two throwaway GitHub repos.

## The fixture token

The `claude plugin eval` sandbox runs each case with a scaffold that has no environment of its own, so the
harness stages a token for it. That token must be a dedicated one. The harness never reads `gh auth token` or
`~/.config/gh/hosts.yml`, and `scaffold.py prepare` refuses to run without `NOT_ME_FIXTURE_TOKEN`.

Create a fine-grained personal access token:

1. GitHub, Settings, Developer settings, Fine-grained tokens, Generate new token.
2. Resource owner: the account that owns the fixture repos. Expiry: 30 days or less.
3. Repository access: **Only select repositories**, then the fixture repo and the companion repo. Nothing else.
   In particular, not this plugin repo.
4. Repository permissions, all Read and write: Contents, Issues, Pull requests, Actions, Workflows.
   Metadata (read) is added for you.
5. Export it for the run: `export NOT_ME_FIXTURE_TOKEN=github_pat_...`

Before staging anything, `prepare` checks the token with `gh api user -H "Authorization: token ..."`. The token
must be able to read the fixture repo (and the companion when the scenario has one) and must not be able to read
this plugin repo (`gh api repos/<plugin repo>` has to fail). A broad token is refused. The plugin repo is taken
from the `origin` remote; set `NOT_ME_PLUGIN_REPO=owner/name` if that is wrong.

The token is copied to `evals/.state/<RID>/<ID>.stage/gh-token` (mode 600) for the scaffold to sign `gh` in
inside the sandbox home, and `scaffold.py clean` deletes it. `run_live.py` runs `clean` after every run, also when
a step failed. If a run is killed, run `python3 evals-harness/scaffold/scaffold.py clean` yourself.

The seeder (`fixture/seed_fixture.py`) still uses your own `gh` login, because it needs rights the fixture token
does not have (it deletes issues and force-moves refs). The eval session only ever sees the fixture token.

## Run a scenario

    export NOT_ME_FIXTURE_TOKEN=github_pat_...
    scripts/eval-live.sh --owner OWNER --repo OWNER/not-me-fixture --companion OWNER/not-me-fixture-companion \
        --skill new --scenario N1 --dry-run

Drop `--dry-run` to run. The phases are described in `evals-harness/README.md`.

## Grading a dry-run (ledger) case

A case that runs in dry-run mode writes its ledger inside the eval workspace, which is deleted afterwards.
Pass `--keep-temp` to keep it. `claude plugin eval` prints the workspace path; the ledger is at

    <workspace>/.git/not-me/logs/intended-writes.jsonl

Run `run_live.py --backend ledger --keep-temp --ledger <that path>`, or grade by hand:

    python3 evals-harness/grade_state.py --manifest evals/.state/RID/N1.json --scenario N1 \
        --backend ledger --ledger <that path>

`--backend ledger` without `--keep-temp` is refused by `run_live.py`.
