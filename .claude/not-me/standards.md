---
schema: 1                        # config format version
# read_first: auto                 # files to read before coding; auto = CONTRIBUTING, CLAUDE.md, README
# branch: "{issue}-{slug}"         # branch name template
# commit: conventional             # conventional | free
pr_title: "{issue_title}"         # PR title template; see PR title below
# pr_template: auto                # path to a PR template; auto = .github/pull_request_template.md if present
# closing_keyword: auto            # keyword that links a PR to its issue; auto = Closes owner/repo#N when tracker is the same repo
# issue_template: auto             # path to an issue template; auto = .github/ISSUE_TEMPLATE if present
---
# Standards

How work is shaped in this project.

## Branch

Branch from the repo's default branch. Name it from the `branch` template.

## Commits

Small commits with a clear summary line. Follow the `commit` style.

## PR title

PR titles must pass `.github/workflows/pr-title.yml`: a Conventional Commit
title, `type(scope): summary`, with type one of feat, fix, docs, refactor,
chore, ci or test. Issue titles here usually start with a type already, so the
title is the issue title. When it has no type, add the one that fits the change.
Never put the issue number first: `40: feat: ...` fails the check.

## PR description

State what changed and why, how it was tested, and link the issue.

## Code style notes

Match the existing code. Run the project's formatter and linter before pushing.

## Issue readiness

An issue is ready to pick up when it has:

- a goal
- an acceptance criteria checklist
- reproduction steps or context
- what is out of scope

## Ticket format

New issues use the same four parts as Issue readiness.

## Delegation

Give these to the most capable model and review them closely:

- UI state that must agree with server rules
- CSS cascade or visual parity
- data migrations and update hooks
- sanitizing or escaping
- security-sensitive code
- the guard: `plugins/not-me/hooks/` (`repo_guard.py`, `hooks.json`) and `plugins/not-me/tests/guard_cases.py`
- GitHub and git calls: `plugins/not-me/notme/gh.py`, `plugins/not-me/notme/git.py`, `plugins/not-me/notme/writes.py`
- config layering and the tighten-only rules: `plugins/not-me/notme/layers.py`
