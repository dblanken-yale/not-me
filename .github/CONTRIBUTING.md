# Contributing

- PR titles must be Conventional Commits: `feat`, `fix`, `docs`, `refactor`, `chore`, `ci`, `test`. Use `feat!:` for a breaking change (major version).
- PRs are squash-merged, so the PR title becomes the commit message.
- release-please computes versions and the changelog. Never hand-edit `version` in `plugin.json` or the manifest.
- Tests must pass on Linux, macOS, and Windows (see `.github/workflows/ci.yml`).
- Keep plugin prose free of organization and personal names. The plugin is project-agnostic.
- Every GitHub write (comments, labels, PRs, reviews) goes through the `notme` CLI, never raw `gh` calls in skills.
