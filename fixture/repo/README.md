# not-me fixture

A throwaway repository used by the not-me eval suite. It holds a tiny stdlib
Python package (`calc/`), a static page (`site/`) and workflows. Nothing here
is real. The eval seeder resets this repo to the `fixture-baseline` tag before
each scenario and may delete its issues, pull requests and branches at any time.

- `make test` runs the unit tests, `make lint` byte-compiles the package.
- `site/index.html` is served with `python3 -m http.server` for browser proofs.
- `.github/workflows/post-as-bot.yml` (stored as `_github/` in the plugin repo) lets the seeder act as `github-actions[bot]`.
  The repo setting "Allow GitHub Actions to create and approve pull requests"
  (Settings, Actions, General, Workflow permissions) must be enabled for the
  `pr` and `review` kinds to work.
- Pull requests created by the bot token do not trigger CI. That is expected.
