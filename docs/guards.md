# Guards

not-me runs unattended, so a mistake can post to the wrong repo or push to the wrong branch before anyone notices. The guard is a hook that checks risky commands first and refuses the ones that break a rule. This page lists the rules, shows what each blocks and allows, and explains how to allow a repo or relax a rule.

The guard is a small safety net for an agent that is trying to follow the rules. It is not a sandbox, and it is deliberately narrow so that not-me can be used to develop itself. Read "Honest limits" before you rely on it, and protect the branches that matter on GitHub too.

## How it works in one minute

- Claude Code calls the hook before every `Bash`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit` and read tool call, and before GitHub MCP tools. Other MCP tools are not judged.
- The hook looks at the call and either lets it through (exit code 0) or blocks it (exit code 2, with a message that goes back to Claude).
- A blocked call does not run. Claude reads the message, does not retry, and records a question for you.
- Every block in a not-me project is also appended to `<git-common-dir>/not-me/logs/guard.jsonl` with the time, tool, rule, reason, and target.

## Activation

The four rule groups only apply when not-me is in play:

- The checkout has a `.claude/not-me/` folder (or, in a linked worktree, the main checkout does), or
- A not-me run is in progress. A run drops a marker file at `<git-common-dir>/not-me/active-run`. A marker older than 6 hours is ignored, so a crashed run cannot keep a repo locked.

Anywhere else (outside any git repo, or in a git repo that is not a not-me project, such as one nested inside a not-me checkout) the rule groups are off and only [self-protection](#self-protection) is checked. Those checks start no process and read no config, and their blocks are not logged.

It is also cheap. A Bash command that mentions none of `git`, `gh`, `rm`, `notme`, a shell or interpreter, a wrapper such as `nohup` or `script`, `$`, backticks, `&`, a path or a glob is allowed after one text check, with no process started and no config read.

## The rule groups

There are four rule groups. Each is on by default. Each is switched by a key under `guard` in `project.md`.

| Rule | In one line |
| --- | --- |
| `guard.other_repo` | git and gh writes may only target repos on the allowed list, and nothing may change where git pushes |
| `guard.protected_branches` | No commits, merges, rebases, cherry-picks or hard resets on protected branches, and no pushes to them |
| `guard.force_push` | No history rewrites and no deleting remote branches |
| `guard.destructive_git` | No changing GitHub settings, secrets or rulesets, no deleting on GitHub, no `rm -rf .git`, `git clean -x` or forced deletes of a protected branch |

Reads are never blocked. `gh pr view`, `git log`, `gh api repos/acme/lib/pulls` and similar work against any repo.

### other_repo

The allowed set is: the repo the checkout belongs to (`origin`), plus `tracker`, plus every repo in `repos`, plus `allow_repos` from your global layer, plus your allow list for this project and for all projects. A git or gh write to anything else is blocked.

Blocked, for example:

```
gh pr comment 5 --repo acme/other --body hi
git push https://github.com/acme/lib.git HEAD
gh api -X POST repos/acme/lib/issues/1/comments -f body=x
gh issue create --repo acme/internal --title x
gh pr create --title x --body y                  # names no repo: gh's default is not always origin
```

It also blocks anything that changes where git pushes:

```
git remote set-url origin https://github.com/acme/lib.git
git config remote.origin.pushurl https://github.com/acme/lib.git
git config --global url.git@github.com:acme/lib.pushInsteadOf https://github.com/acme/app
git -c remote.origin.pushurl=git@github.com:acme/lib.git push
GIT_DIR=../other/.git git push                  # also GIT_WORK_TREE, GIT_CONFIG_*, HOME, --git-dir, --work-tree
```

The keys that count are `remote.*.url`, `pushurl`, `push` and `mirror`, `url.*.insteadOf` and `pushInsteadOf`, `remote.pushDefault`, `branch.*.remote`, `pushRemote` and `merge`, `push.default`, `push.recurseSubmodules`, `include.path`, `includeIf.*.path`, and `alias.*` (an alias can hide a push). Edits to `.git/config`, `.git/hooks/*` and `~/.gitconfig` with the file tools are blocked too.

A git alias is looked up and judged as the command it stands for; a shell alias (`!...`) is judged as [opaque text](#opaque-text). A program the guard does not know that is handed `git` or `gh` as a word (`watch git push`, `flock x git push`) is judged as that git or gh command.

Allowed, for example:

```
gh pr view 5 --repo acme/other                  # reads work anywhere
gh pr comment 5 --repo acme/app --body hi       # the checkout's own repo
git push -u origin feature
FOO=1 git push origin feature                   # an unrelated variable is fine
git -C ../app-ui commit -F msg.txt              # the checkout is named
cd sub && git commit -m x                       # judged in the session's checkout; see Honest limits
```

### protected_branches

Protected branches are `protected_branches` from the config. The default is `main`, `master`, and `develop`. The hook compares the exact branch name the command targets.

Blocked, for example:

```
git commit -m x                               # while checked out on main
git merge feature/x                           # while checked out on main (also rebase, cherry-pick, am)
git reset --hard origin/main                  # while checked out on main
git push origin HEAD:main
git push origin feature/x:refs/heads/develop
git push --all origin
git push --mirror
git push                                      # when the upstream or push config sends it to main
git push origin $(git branch --show-current)  # a branch the shell fills in cannot be checked
```

#### Merging pull requests

The guard does not block merging a pull request: `gh pr merge`, the merge endpoints and the GitHub MCP merge tool pass (a merge in a repo off the allowed list is still blocked by `other_repo`). That is a policy, not a gap: no not-me skill merges, merging is not part of any skill's flow, and autonomous runs never merge (see the prohibitions every skill follows). A person merges, by hand or by asking Claude directly in a session. If you want GitHub to hold that line, require a pull request review on your protected branches (see [Honest limits](#honest-limits)).

Merging is allowed, but `gh pr merge` or `gh pr close` with `--delete-branch` (or `-d`) is blocked under `force_push`, because it deletes the remote branch (a PR from `develop` into `main` would delete `develop`). Delete a branch by hand.

Allowed, for example:

```
git checkout -b feature/y && git commit -m x  # branch first, then commit
git merge main                                # on a feature branch
git rebase origin/main                        # on a feature branch
git merge --abort                             # on main, to back out
git reset --soft HEAD~1                       # on main: only --hard is blocked
git status
```

### force_push

Blocked, for example:

```
git push --force
git push -f origin feature/x
git push --force-with-lease origin feature/x
git push --forc origin feature/x              # git accepts abbreviated options, and so does the check
git push origin +feature/x
git push --delete origin feature/x
git push origin :feature/x
```

Allowed: a normal push that adds commits, such as `git push -u origin feature/x`.

### destructive_git

Shown in the block message as `destructive`.

Blocked, for example:

```
gh secret set TOKEN --repo acme/app --body x
gh variable set X --repo acme/app --body y
gh ruleset view 1 --repo acme/app
gh api -X PUT repos/acme/app/branches/main/protection --input p.json
gh release delete v1 --repo acme/app
gh repo edit acme/app --visibility public
gh repo delete acme/app --yes
gh issue delete 1 --repo acme/app --yes
gh api -X DELETE repos/acme/app/labels/x
rm -rf .git
git clean -fdx
git branch -D main
```

GitHub settings, secrets, variables, rulesets, branch protection, release deletes and deleting anything on GitHub are always treated as human actions.

Allowed: `git reset --hard HEAD~1` on a feature branch, `git branch -D feature/old`, `git clean -fd`, and `rm -rf node_modules build`.

## Opaque text

Some text the guard does not parse: a command it cannot read (an unclosed quote, more than 32 KB, nesting deeper than 64 levels), a command name the shell fills in (`$GIT`, `{git,push}`), inline interpreter code (`python3 -c`, `node -e`), code an interpreter reads from a pipe or a here-document, a script file (`bash x.sh`, `./x.sh`, `python3 x.py`, `source x`), a shell alias, and the words `xargs` or `parallel` add from their input. Where it can, the guard parses the inner command and judges it like any other: the commands inside `$(...)` and backticks, `bash -c` and `eval` bodies (up to 4 levels deep), a here-document fed to a shell, `find -exec`, `sudo`, `script -c`, `git rebase --exec` and `git submodule foreach`.

Opaque text is allowed unless its decoded text (quotes and backslashes removed, `\x67`-style escapes decoded, any case) holds one of these, in which case the call is blocked under the first of the listed rules that is on:

| Text | Rule it reports |
| --- | --- |
| `push` as a word | `protected_branches`, else `force_push`, else `other_repo` |
| `reset --hard` | `protected_branches` |
| `-X DELETE`, `--method DELETE`, `secret set`, `secret delete`, `secret remove`, a secrets API path (`/actions/secrets`, `/dependabot/secrets`, `/codespaces/secrets`, `/environments/<name>/secrets`), `branch -D`, `clean -x` (also `-fdx`), `rm -r... .git` | `destructive_git` |
| `operator-token`, `mode-live.sig`, `mode-live.epoch`, `allowed-repos.json` | self-protection, always |
| `allow-repo add` (or any argument but `list`), `pause off`, `mode live`, when the text sits in the command itself rather than in a script file | self-protection, always |

So:

```
python3 -c 'print(1)'                           # allowed: nothing in the list
shellcheck $(git ls-files '*.sh')               # allowed: the inner git ls-files is a read
bash -c 'git log --oneline -5'                  # allowed: parsed, a read
bash -c "git push origin main"                  # blocked: parsed, a push to main
python3 -c 'import os; os.system("git push")'  # blocked: inline code that holds push
curl -s https://example.com/install.sh | sh     # allowed: the pipe's text holds nothing in the list
sh scripts/release.sh                           # blocked when the script holds push
```

With all four rules off, opaque text is blocked only for the self-protection words.

## The block message

Every block says which rule fired, what was blocked, and what to do. This is the message for a write to a repo that is not allowed:

```
not-me guard [other_repo]: blocked `gh pr comment 5 --repo acme/other --body hi`: targeting acme/other.
This repo (acme/app) may write only to: acme/app.
If acme/other is intended, the operator runs:  /not-me:allow-repo acme/other
  (all projects: /not-me:allow-repo acme/other --everywhere; team-wide: list it under repos in .claude/not-me/project.md, then each operator confirms it once with /not-me:allow-repo acme/other)
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

A git write whose `-C`, `--git-dir` or `--work-tree` path is a shell variable (`git -C "$W" commit ...`) is blocked, because the guard does not expand variables. Rewriting the command is the fix, so that message leaves out the "relax this rule" line:

```
not-me guard [other_repo]: blocked `git -C $W commit -m x`.
Why: the guard does not expand shell variables in a `git -C` path.
Instead: write the path out in full (`git -C /abs/path <sub> ...`); a rewritten command is checked afresh, so this is not a retry.
Autonomous runs: do not retry; log a QUESTION naming the operator and continue.
See effective config: /not-me:config
```

For `--git-dir` or `--work-tree` the Instead line says to drop that option and use `git -C /abs/path` with the path written out, since a literal `--git-dir` is blocked too. A git alias next to a variable path is blocked the same way, because the guard cannot look the alias up in a repo it cannot name; if the path resolves to the current checkout (`git -C "$W/.." fp`) and the alias is defined there, the alias's own rules (force push, protected branch) block first. Blocks that do not come from the unknown path, such as `git -C "$W" remote set-url ...` or a `GIT_DIR=` assignment, keep their own message.

One detail in the message is misleading. It names `.claude/not-me/project.md` for "relax this rule", but the repo layer ignores that (see below), so use your global layer, `~/.claude/not-me/project.md`.

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
- `protected_branches: off` allows commits and pushes on `main` and the other protected branches.
- `force_push: off` allows history rewrites and remote branch deletion.
- `destructive_git: off` allows changing repo settings and secrets, and deleting data.
- A block is usually a sign the run is doing something you did not intend. Allowing one repo is nearly always the better fix.

## Self-protection

Self-protection is always on, in every directory, whether or not the guard is active there. It does not depend on the four rules and cannot be switched off. It blocks:

- By file path: reading or changing the operator token or the live-mode epoch (`mode-live.epoch`), and changing the live-mode marker (`mode-live.sig`) or the allow list (`allowed-repos.json`). This covers the Read, Grep, Glob and LS tools, Write and Edit, and Bash words and redirections that name one of those files after a path separator, resolve to it, or reach it through a glob or a part the shell fills in (`$VAR`, `$(...)`, backticks, `{a,b}`) when the rest of the path pins it there. A recursive copy or search (`cp -r`, `tar`, `rsync`, `grep -r`, `rg`) of a directory that holds the not-me home counts too.
- As parsed commands: `notme allow-repo` with anything but `list` (`add`, `remove`, `--everywhere`), `notme pause off`, and `notme mode` with anything but `show` or `dry-run`, however notme is started (`notme`, `bin/notme`, `bash bin/notme`, `python3 cli/notme.py`, `python3 -m notme.cli` or `-m notme`, also after python options such as `-W ignore` or `-X utf8`). Without the signed marker a `mode: live` setting runs as dry-run, so no file edit can turn live mode on; going back to dry-run with `/not-me:mode dry-run --global` replaces the epoch, so a saved copy of an old signature stops working.
- A pseudo-terminal or detached process around notme: `script`, `expect`, `unbuffer`, `socat ... pty`, `tmux send-keys`, `screen -X stuff`, `ssh -t localhost`, `nohup`, `setsid`, `disown`, `at`, `batch`, `systemd-run`, `daemon`, `launchctl submit`, `crontab` and a background `&`, when the command or a file it is given names `notme`. Those would pass the CLI's own human check. Around anything else they are allowed.
- A Write or Edit that would leave `mode: live` (or anything but `dry-run`) in your global `project.md`. This one is a courtesy: it stops the attempt early with a pointer to `/not-me:mode live`. The signature above is what actually keeps live mode off.
- Writes to the installed plugin's own files: its `hooks`, `bin`, `notme`, `cli`, and `skills` folders.
- Writes to not-me config while a run is in progress: the repo's `.claude/not-me/` folder and the `.md` files in your global folder.

The block message in each case says to ask the operator, which is you.

## When the guard itself breaks

- If the hook crashes, it reads its raw input as [opaque text](#opaque-text): input that holds one of those words is blocked with `not-me guard failed closed: <error>; run /not-me:doctor`, anything else is let through.
- A check that runs longer than 8 seconds blocks the call, so a slow check cannot run into the hook's 10 second timeout, which would let the call through.

A missing Python is the case the hook cannot report itself. The launcher exits with code 7, and Claude Code treats any exit code other than 2 as a non-blocking error, so the call would run unchecked. To catch this, every autonomous run starts with a self-test. It pipes a command that must always be blocked (a write to the allow list) through the real hook command and checks that the exit code is 2. If it is not, the run stops with `FAILED - guard not working`. `/not-me:doctor` runs the same test.

## Honest limits

The guard splits a command into words the way the shell does: quotes, backslashes, line continuations, `$(...)`, backticks, here-documents and redirections. It does not run anything, expand variables or follow `cd`. It is a net for an agent that follows the rules, kept small on purpose: an earlier version tried to prove every command safe and blocked so much ordinary work that not-me could not develop itself. The trade-off is that a determined or buggy program can get around it.

What it does not cover:

- `cd`. A git write is judged in the session's working directory, or the `git -C` / `gh -R` it names. `cd ../other && git push` is judged as a push from the session's checkout. Name the checkout with `git -C` instead.
- Programs it does not read. A script file is scanned only for the opaque-text words, the modules a script imports and the file arguments of `python3 -m` are not read, a file written and run in the same command is read before the write, and a script that builds its command from pieces will not match. Programs that run commands from their own arguments (`awk 'BEGIN{system(...)}'`, `sed e`, `vim -c '!...'`, `tar --to-command`, `less +!`) are not inspected.
- Merges. The guard does not block them; the skills do not merge. See [Merging pull requests](#merging-pull-requests).
- Pushes and writes through other tools. `curl` or any HTTP client to the GitHub API, a gh extension or alias, a git hook, and an external `git-<name>` program are outside its view. So are MCP tools other than GitHub ones, including terminal and session tools, and your own terminal.
- Opaque text that names a gh write without one of the opaque-text words (`$GH pr comment 1 -R acme/other`): the other-repo check needs a command it can read.
- Reads. Any repo your token can read can be read.
- Time of check. It looks at your git config when it checks. Something that changes the config between the check and the command is out of reach.
- The repo layer's `tracker` and `repos`. The hook allows the repos named there without looking at your allow list, while the `notme` CLI requires you to confirm them. Review changes to those two keys the way you would review code.

Use the guard with the protections that do not depend on it:

- On GitHub, add a ruleset (or branch protection) to every protected branch of every repo not-me works in: restrict deletions, block force pushes, and require a pull request (with a review, if you want a person on every merge) before merging. That is the control a script cannot route around.
- A token with the narrowest scopes that work.
- A person who decides what merges.
