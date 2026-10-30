---
schema: 1                        # config format version
baseline: always                 # always | auto; auto skips the full baseline when the base branch's CI is green at its head
---
# Verify

How to prove a change works before asking for review. Leave a section as is to
let not-me discover the commands; write commands in it to pin them.

## Environment start

Start whatever the project needs to run locally. Discover it from the README,
docker-compose files or a Makefile.

## Baseline

Run the tests once before changing anything and record failures that already
exist, so they are not blamed on the change. With `baseline: auto` the run skips
this when the base branch's CI is green at its head, and logs that it did.

## Targeted tests

Commands that run only some tests: the changed test files or test names go at
the end of the command. Runs use them for red then green, after the implementer
and after each fix round, and run the full Tests, Lint and Build once before the
push. Left as is, none is configured and every step runs the full commands. To
pin one, write it in a fenced code block here, one command per line.

## Tests

Discover the test command from package.json, pyproject.toml, composer.json, a
Makefile, or the run steps in `.github/workflows/*.yml`. Log what was found.

## Lint

Discover the lint command the same way. Prefer the command CI runs.

## Build

Run the build if the project has one.

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

Commands a run never starts, such as live evals or anything that calls a paid
model or service for real. A person runs them. None by default.
