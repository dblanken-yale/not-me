# Guards

not-me runs unattended, so a mistake can post to the wrong repo or push to the wrong branch before anyone notices. The guard is a hook that checks every risky command first and refuses the ones that break a rule. This page lists the rules, shows what each blocks and allows, and explains how to allow a repo or relax a rule.

The guard is a safety net for an agent that is trying to follow the rules. It is not a sandbox. Read "Honest limits" before you rely on it.

## How it works in one minute

- Claude Code calls the hook before every `Bash`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit`, and GitHub MCP tool call.
- The hook looks at the call and either lets it through (exit code 0) or blocks it (exit code 2, with a message that goes back to Claude).
- A blocked call does not run. Claude reads the message, does not retry, and records a question for you.
- Every block in a not-me project is also appended to `<git-common-dir>/not-me/logs/guard.jsonl` with the time, tool, rule, reason, and target.

## Activation

The four rule groups only apply when not-me is in play:

- The checkout has a `.claude/not-me/` folder (or, in a linked worktree, the main checkout does), or
- A not-me run is in progress. A run drops a marker file at `<git-common-dir>/not-me/active-run`. A marker older than 6 hours is ignored, so a crashed run cannot keep a repo locked.

Anywhere else (outside any git repo, or in a git repo that is not a not-me project, such as one nested inside a not-me checkout) the rule groups are off and only [self-protection](#self-protection) is checked. Those checks start no process and read no config, and their blocks are not logged.

It is also cheap. A Bash command that mentions none of `git`, `gh`, `rm`, `curl`, a shell or interpreter, `$`, backticks, or a script path is allowed after one text check, with no process started and no config read. Ordinary commands cost tens of milliseconds.

## The rule groups

There are four rule groups. Each is on by default. Each is switched by a key under `guard` in `project.md`.

| Rule | In one line |
| --- | --- |
| `guard.other_repo` | Writes may only target repos on the allowed list |
| `guard.protected_branches` | No commits, pushes, merges, or resets on protected branches, and no merging pull requests |
| `guard.force_push` | No history rewrites and no deleting remote branches |
| `guard.destructive_git` | No hard resets of protected branches, forced branch deletes, `clean -x`, deleting a checkout, or changing GitHub settings, secrets, or data |

Reads are never blocked. `gh pr view`, `git log`, `gh api repos/acme/lib/pulls` and similar work against any repo.

### other_repo

The allowed set is: the repo the checkout belongs to (`origin`), plus `tracker`, plus every repo in `repos`, plus `allow_repos` from your global layer, plus your allow list for this project and for all projects. Anything else is blocked.

Blocked, for example:

```
gh pr comment 5 --repo acme/other --body hi
git push https://github.com/acme/lib.git HEAD
gh api -X POST repos/acme/lib/issues/1/comments -f body=x
git remote set-url origin https://github.com/acme/lib.git && git push
gh issue create --repo acme/internal --title x
```

It also blocks anything that could change where git pushes, and anything it cannot read well enough to check:

```
git config remote.origin.pushurl https://github.com/acme/lib.git
git -c remote.origin.pushurl=git@github.com:acme/lib.git push
bash -c "gh pr comment 5 --repo acme/lib --body x"
echo $(gh pr comment 1 -R acme/other -b hi)
python3 -c 'import subprocess; subprocess.run(["gh", "pr", "comment", "5", "-R", "acme/other", "-b", "x"])'
```

Edits to `.git/config`, `.git/hooks/*`, and `~/.gitconfig` are blocked too, because they change where git sends things.

Allowed, for example:

```
gh pr view 5 --repo acme/other                  # reads work anywhere
gh api repos/acme/lib/pulls
gh pr comment 5 --repo acme/app --body hi       # the checkout's own repo
git push -u origin feature
python3 -c 'print(1)'                           # inline code that does not touch git or GitHub
sh scripts/clean.sh                             # a script that does not mention git or gh
git commit -m "$(cat <<'EOF'
fix: x

git push is described here
EOF
)"                                              # heredoc text is data
```

### protected_branches

Protected branches are `protected_branches` from the config. The default is `main`, `master`, and `develop`. The hook compares the exact branch name the command targets.

Blocked, for example:

```
git commit -m x                               # while checked out on main
git push origin HEAD:main
git push origin feature/x:refs/heads/develop
git merge feature/x                           # while checked out on main
git push --all origin
gh pr merge 1 --repo acme/app --squash
```

`gh pr merge` and the merge endpoint (`gh api -X PUT repos/acme/app/pulls/1/merge`) are blocked whatever the branch, because a person merges.

Allowed, for example:

```
git checkout -b feature/y && git commit -m x  # branch first, then commit
git merge main                                # on a feature branch
git rebase origin/main                        # on a feature branch
git merge --abort                             # on main, to back out
git status
```

### force_push

Blocked, for example:

```
git push --force
git push -f origin feature/x
git push --force-with-lease origin feature/x
git push origin +feature/x
git push --delete origin feature/x
git push origin :feature/x
gh pr merge 1 --admin --repo acme/app
```

Allowed: a normal push that adds commits, such as `git push -u origin feature/x`.

### destructive_git

Shown in the block message as `destructive`.

Blocked, for example:

```
git reset --hard origin/main                  # while checked out on main
git branch -D main
git clean -fdx
rm -rf .git
git push --mirror
gh issue delete 1 --repo acme/app --yes
gh repo edit acme/app --visibility public
gh secret set TOKEN --repo acme/app --body x
gh api -X DELETE repos/acme/app
```

Settings, secrets, variables, rulesets, branch protection, releases, labels, and deleting anything on GitHub are always treated as human actions.

Allowed: `git reset --hard HEAD~1` on a feature branch, `git branch -D feature/old`, `git clean -fd`, and `rm -rf node_modules build`.

## The block message

Every block says which rule fired, what was blocked, and what to do. This is the message for a write to a repo that is not allowed:

```
not-me guard [other_repo]: blocked `gh pr comment 5 --repo acme/other --body hi`: targeting acme/other.
This repo (acme/app) may write only to: acme/app.
If acme/other is intended, the operator runs:  /not-me:allow-repo acme/other
  (all projects: /not-me:allow-repo acme/other --everywhere; team-wide: add it to allow_repos in .claude/not-me/project.md)
Autonomous runs: do not retry; log a QUESTION naming the operator and continue.
Relax this rule (not recommended): set guard.other_repo: off and confirm_guard_off: [other_repo] in .claude/not-me/project.md
See effective config: /not-me:config
```

The other rules print a shorter form, with a reason and an alternative instead of the allow command:

```
not-me guard [protected_branches]: blocked `git commit -m x`.
Why: commits on the protected branch main are not allowed.
Instead: create a feature branch first (git switch -c <type>/<topic>) and commit there.
Autonomous runs: do not retry; log a QUESTION naming the operator and continue.
Relax this rule (not recommended): set guard.protected_branches: off and confirm_guard_off: [protected_branches] in .claude/not-me/project.md
See effective config: /not-me:config
```

Two details in the message are misleading. The message names `.claude/not-me/project.md` for both "team-wide `allow_repos`" and "relax this rule". The repo layer ignores both (see below), so use your global layer, `~/.claude/not-me/project.md`, for relaxing a rule. For `allow_repos` use the global layer too, or better, use `/not-me:allow-repo`.

## Allow a repo

Only you can do this. The agent cannot, because the guard blocks it from running the command or editing the file.

```
/not-me:allow-repo acme/friend
```

This allows `acme/friend` for the project you are standing in (it is recorded against this checkout's `origin`). Other options:

```
/not-me:allow-repo acme/friend --everywhere     # every project on this machine
/not-me:allow-repo --board acme/7               # a Projects v2 board, owner/number
```

The entries live in `~/.claude/not-me/allowed-repos.json`:

```json
{
  "projects": {
    "acme/app": {"allow": [{"repo": "acme/friend", "added": "2026-10-21T10:00:00Z"}]},
    "*": {"allow": [{"board": "acme/7", "added": "2026-10-21T10:05:00Z"}]}
  },
  "version": 1
}
```

- The key is the project's `origin` repo, or `*` for everywhere.
- A repo entry must be a plain `owner/name`. URLs and wildcards are rejected. Matching ignores upper and lower case.
- To remove an entry, delete it from the file in your own editor.
- If the file is not valid JSON, the guard ignores it, and only the checkout's own repo, `tracker`, `repos`, and global `allow_repos` are allowed. The `notme` CLI says so in its warnings.

`/not-me:setup` offers to do this for the companion repos and board that it writes into the repo layer.

## Relax a rule

You usually should not. If you must, do it in your global layer, `~/.claude/not-me/project.md`, and confirm it in the same file:

```yaml
---
schema: 1
guard: {force_push: off}
confirm_guard_off: [force_push]
---
```

Both lines are needed. An `off` that is not listed in `confirm_guard_off` in the same file is ignored, and `/not-me:config` reports it. An `off` in the repo layer is always ignored, because anyone with push access could otherwise switch the guard off for the whole team.

Why you usually should not:

- It applies to every repo on your machine, not just one.
- `other_repo: off` lets not-me write to any repo your token can reach.
- `protected_branches: off` also stops blocking `gh pr merge`, so a run could merge.
- `force_push: off` allows history rewrites and remote branch deletion.
- `destructive_git: off` allows changing repo settings and secrets, and deleting data.
- When a command is too opaque to check (`bash -c ...`, `$(...)`), the guard reports it under the first rule that is still on. With all four off, those commands are allowed.
- A block is usually a sign the run is doing something you did not intend. Allowing one repo is nearly always the better fix.

## Self-protection

Self-protection is always on, in every directory, whether or not the guard is active there. It does not depend on the four rules and cannot be switched off. It blocks:

- Any Write or Edit of `allowed-repos.json`, and any Bash command that mentions that file or its full path.
- Any Bash command containing `allow-repo` together with `add`, `remove`, or `--everywhere`, for example `notme allow-repo add acme/other`.
- Turning a pause off (`notme pause off`), and driving the not-me CLI through a pseudo-terminal or a detached process.
- Reading, listing, copying, or writing the operator token in your global folder, by any tool, glob, or redirection.
- Writes to the plugin's own code: its `hooks`, `bin`, `notme`, `cli`, and `skills` folders.
- Writes to not-me config while a run is in progress: the repo's `.claude/not-me/` folder and the `.md` files in your global folder.

The block message in each case says to ask the operator, which is you.

## When the guard itself breaks

The guard fails closed:

- If the hook crashes on a call that mentions `gh`, `git`, GitHub, `.git/`, or the allow list, it blocks the call. The message is `not-me guard failed closed: <error>; run /not-me:doctor`. A crash on a call that has nothing to do with git or GitHub lets the call through.
- Anything the hook cannot read well enough to check is blocked, not guessed at.

A missing Python is the case the hook cannot report itself. The launcher exits with code 7, and Claude Code treats any exit code other than 2 as a non-blocking error, so the call would run unchecked. To catch this, every autonomous run starts with a self-test. It pipes a command that must always be blocked (a write to the allow list) through the real hook command and checks that the exit code is 2. If it is not, the run stops with `FAILED - guard not working`. `/not-me:doctor` runs the same test.

## Honest limits

The guard reads the text of a command with a simple tokenizer. It splits on `&&`, `||`, `;`, `|`, `&`, and newlines, then on spaces, and removes quotes and redirections. It understands `git`, `gh`, `rm`, `find`, `xargs`, `sudo`, `env`, `time`, `timeout`, and a few more. It is a net for an agent that follows the rules. A determined or buggy program can get around it.

What it does not cover:

- Programs and scripts it cannot read. A shell script is scanned for `git`, `gh`, and `github.com`, but a file run as `python3 tool.py` is not opened, and a script that builds its command from pieces will not match.
- Tools outside its matcher. Only Bash, the file-editing tools, and MCP tools with `github` in the name are checked. Other MCP servers and your own terminal are not.
- Reads. Any repo your token can read can be read.
- Time of check. It looks at your git config when it checks. Something that changes the config between the check and the command is out of reach.
- The repo layer's `tracker` and `repos`. The hook allows the repos named there without looking at your allow list, while the `notme` CLI requires you to confirm them. Review changes to those two keys the way you would review code.

The cost of its caution is false alarms: it blocks constructs it cannot see through, even when they are harmless. When that happens, run the `git` or `gh` command directly, without a wrapper, and it will be checked normally.

Use the guard with the usual protections: a token with the narrowest scopes that work, branch protection on the repos that matter, and a person who merges.
