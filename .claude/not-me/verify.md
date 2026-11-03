---
schema: 1                        # config format version
baseline: auto                   # skip the full baseline when the base commit's CI is green
---
# Verify

How to prove a change works before asking for review. Leave a section as is to
let not-me discover the commands; write commands in it to pin them.

## Environment start

Nothing to start. not-me is a plugin and a stdlib-only Python package; the commands below run straight from the checkout.

## Baseline

Run the tests once before changing anything and record failures that already
exist, so they are not blamed on the change.

## Targeted tests

Run only the test files for the modules a change touches; add the guard cases when guard code changes:

```bash
python3 scripts/run-tests.py --suite plugin -k
```

Pass the changed test modules as -k patterns (for example `-k tests.test_git.`), one -k per module. For scripts, docs, fixture or harness tests use `--suite scripts` (or docs, fixture, harness) the same way. When plugins/not-me/hooks/ or plugins/not-me/tests/guard_cases.py change, also run `python3 plugins/not-me/tests/run_guard_cases.py`.

## Tests

Pinned from CLAUDE.md and `.github/workflows/ci.yml`:

```bash
python3 scripts/run-tests.py
python3 plugins/not-me/tests/run_guard_cases.py
```

`scripts/run-tests.py` runs only unit tests, in parallel (about 30 s).

## Lint

Pinned from CLAUDE.md and `.github/workflows/ci.yml`:

```bash
python3 scripts/check-skills.py
python3 scripts/gen-cli-docs.py --check
shellcheck $(git ls-files '*.sh')
```

## Build

Pinned from CLAUDE.md and `.github/workflows/ci.yml`:

```bash
claude plugin validate --strict plugins/not-me
bash scripts/validate-plugins.sh
```

## CI parity

Run the same commands CI runs, in the same order, before claiming a green result.

## Health check

After starting the app, confirm it responds.

## Reset

Return the environment to a clean state after verifying.

## Visual and accessibility

For UI changes, look at the result and check basic accessibility.

## Gate additions

Extra checks this project requires before review. None by default.

## Person only

`scripts/eval-live.sh` and `evals-harness/run_live.py` (live evals). A person runs them after a release.
