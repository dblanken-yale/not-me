# Releases

## How releases work

Versions and the changelog are produced by [release-please](https://github.com/googleapis/release-please). Every PR is squash-merged with a Conventional Commit title:

- `feat:` bumps the minor version.
- `fix:` bumps the patch version.
- `feat!:` or a `BREAKING CHANGE:` footer bumps the major version.
- `docs:` and `refactor:` appear in the changelog; `chore:`, `ci:`, and `test:` are hidden.

On each push to `main`, release-please opens or updates a release PR. Merging it tags the release as `not-me-vX.Y.Z`, updates `plugins/not-me/CHANGELOG.md`, and writes the new version into `plugins/not-me/.claude-plugin/plugin.json`. Never edit `version` by hand.

`main` is always installable: CI must be green before merge, and the marketplace points at `./plugins/not-me`.

## Updating

Users run:

```
/plugin update not-me@not-me
```

## After a release: live evals (optional, a person runs them)

The live evals call a model for real and take a while, so CI and the plugin's own runs never start them. After a release, a person may run them by hand against the fixture repos to check the skills still behave (flags and setup: `evals-harness/README.md`):

- `scripts/eval-live.sh`
- `evals-harness/run_live.py`

They are optional. A release does not wait on them.

## Config schema policy

Repo config docs carry a `schema` number.

- A change that breaks existing skill behavior is a major version.
- A new schema number ships with a migration in `notme/migrations.py` so existing configs upgrade automatically (and `/not-me:doctor` reports any that cannot).
