---
schema: 1                        # config format version
---
# Verify

How to prove a change works before asking for review. Leave a section as is to
let not-me discover the commands; write commands in it to pin them.

## Environment start

Nothing to start. not-me is a plugin and a stdlib-only Python package; the commands below run straight from the checkout.

## Baseline

Run the tests once before changing anything and record failures that already
exist, so they are not blamed on the change.

## Tests

Pinned from CLAUDE.md and `.github/workflows/ci.yml`:

```bash
python3 scripts/run-tests.py
python3 plugins/not-me/tests/run_guard_cases.py
```

Do not run the live evals (`scripts/eval-live.sh`, `evals-harness/run_live.py`); they take too long. `scripts/run-tests.py` runs only unit tests.

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
