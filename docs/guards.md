# Guards

not-me runs unattended, so a mistake can push to the wrong branch or delete something before anyone notices. The guard is a hook that checks risky commands first. It hard-blocks only what breaks the process or cannot be undone. Everything else it guides: the call runs, the guard logs it, and the process rule lives in the skills and [`references/prohibitions.md`](../plugins/not-me/references/prohibitions.md). This page lists the rules, shows what each blocks, guides and allows, and explains how to relax a rule.

The guard is a small safety net for an agent that is trying to follow the rules. It is not a sandbox, and it is deliberately narrow so that not-me can be used to develop itself and does not get in the operator's way. Read "Honest limits" before you rely on it, and protect the branches that matter on GitHub too.

## How it works in one minute

- Claude Code calls the hook before every `Bash`, `Write`, `Edit`, `MultiEdit` and `NotebookEdit` call. Read tools (`Read`, `Grep`, `Glob`, `LS`) are not hooked: nothing the guard protects is secret. GitHub MCP tools (`mcp__*github*`) get the hard rules only (deletes and settings, writes and merges that land on a protected branch); other MCP tools are not judged, and no MCP tool gets the other-repo check. You decide which MCP tools to install and allow.
- The hook looks at the call and either lets it through (exit code 0) or blocks it (exit code 2, with a message that goes back to Claude).
- A blocked call does not run. Claude reads the message, does not retry, and records a question for you. In a not-me run a block is a failure on not-me's side: the run posts nothing, changes no labels, gives back its claim and stops with `FAILED <ref> blocked - <target>` (see "When a person is needed" in [workflow.md](workflow.md)).
- A guided call runs. The guard found something a run should not do on its own (a write to a repo outside the project, say), so it logs it; the skills and references tell the run not to do it, and a person may.
- Every block and every guided call in a not-me project is appended to `<git-common-dir>/not-me/logs/guard.jsonl` with the time, `outcome` (`blocked` or `guided`), tool, rule, reason, target, and `input`: the call (the Bash command, the file path for file tools, else the tool's arguments without any content body at any depth). `input` is redacted before it is written (values of variables, flags and argument keys named like a token, key, secret or password, the `--body`/`-b` or `<<<` value of `gh secret set` (not a value piped in), `Authorization` header values, passwords in URLs and in `curl -u user:pass`, and GitHub token shapes) and cut to 2000 characters. The file stays on your machine; nothing posts it to GitHub. One call writes at most one line.

## Activation

The rule groups only apply when not-me is in play:

- The checkout has a `.claude/not-me/` folder (or, in a linked worktree, the main checkout does), or
- A not-me run is in progress. A run drops a marker file at `<git-common-dir>/not-me/active-run`. A marker older than 6 hours is ignored, so a crashed run cannot keep a repo locked.

Anywhere else (outside any git repo, or in a git repo that is not a not-me project, such as one nested inside a not-me checkout) the rule groups are off and only [self-protection](#self-protection) is checked. Those checks start no process and read no config, and their blocks are not logged.

It is also cheap. A Bash command that mentions none of `git`, `gh`, `rm`, a copy, move, link or extract program (`cp`, `mv`, `ln`, `rsync`, `install`, `ditto`, `tar`, `unzip`), a shell or interpreter, `$`, backticks, `&`, a path, a glob or the allow list's name is allowed after one text check, with no process started and no config read.

## Audit: what blocks, what guides

Decided by the owner on 2026-10-30 (dblanken-yale/not-me#141): guide runs to the right process and state; hard-block only what breaks the process or cannot be undone. **Keep** is a hard block. **Guide** means the call runs, the guard logs it as `guided`, and a process rule in the references covers it. **Remove** means neither. Rows naming dblanken-yale/not-me#118 were reshaped by that change (owner, 2026-10-30: operator-invoked allow-list, pause and mode changes run in the session).

### Guard rules

| Rule | Outcome | Why |
| --- | --- | --- |
| Commits, merges, rebases, cherry-picks, `am` and `git reset --hard` on a protected branch | keep | Breaks the process: work reaches protected branches only through a reviewed PR, and a hard reset throws work away. |
| Pushes to a protected branch, `--all`, `--mirror`, a bare push whose upstream is protected, a refspec the shell fills in | keep | Same: lands on a protected branch without review. |
| A push whose target the guard cannot see: `git -c` push or alias keys on a push, an alias or a git that runs other gits (`rebase`, `submodule`, `bisect`, `filter-branch`, `for-each-repo`, `difftool`, `mergetool`, `maintenance`, `worktree`); `GIT_DIR`/`GIT_CONFIG_*`/`--git-dir`/`--work-tree`/`HOME` on a write or on one of those; `git send-pack`, `git subtree push`, pushing submodules | keep (now `protected_branches`, was `other_repo`) | The branch it lands on cannot be checked, so it may be protected. A child git inherits `-c` and the variables. |
| A git write through a `--git-dir`/`--work-tree` path the shell fills in | keep (now `protected_branches` first) | The checkout it changes may be on a protected branch. |
| A git write through a `-C` path, or after a leading `cd`, that the guard cannot work out | guide (changed by dblanken-yale/not-me#144; was keep) | Judging it by the session's directory blocked a run's commit in its own worktree. A variable set to a literal path earlier in the command is filled in, and a leading `cd <path>` is followed. A protected branch the command names, and a force push, still block. |
| A ref update, contents write or merge without a pull request through the GitHub API, to a protected branch, the default branch or a branch the guard cannot read (`PATCH .../git/refs/heads/main`, `PUT .../contents/...` with no feature branch, `POST .../merges` and `.../merge-upstream` into a protected `base`/`branch`, any `@file` field value; `refs/heads/main`, `heads/main`, `refs/main` and trailing slashes all count as `main`, and any other ref under `refs/` (a tag, a remote) cannot be checked, so it blocks; the same for GitHub MCP tools; GraphQL `updateRef(s)`, `createRef`, `createCommitOnBranch`, `mergeBranch`, `revertPullRequest`) | keep (new) | A push in all but name. Before, only writes to repos off the allowed list were stopped. |
| Force pushes, `+` refspecs, forced API ref updates, remote branch deletes, `gh pr merge\|close --delete-branch` | keep | Cannot be undone. |
| GitHub settings, secrets, variables, rulesets and branch protection; repo edit, rename, archive and delete; release, label and issue deletes; `gh workflow disable`; any API `DELETE` | keep | Settings and secrets are a person's call, and deletes cannot be undone. |
| GraphQL mutations: deletes and settings (`delete*`, branch protection, rulesets, secrets, environments, deploy keys, `updateRepository`, ...), any mutation field not on a short safe list (comments, reactions, labels, assignees, issues, pull requests, reviews and review threads, sub-issues, board items, merging a pull request), and a query the guard cannot read (not one literal query, an unterminated string or block string, unbalanced brackets, a fragment spread in a mutation) | keep (new for literal mutations; an unreadable query moved from `other_repo`) | Same as above, through the GraphQL API. The query is read the way GraphQL reads it (`#` comments and commas skipped, aliases and directives understood, strings opaque, and in a block string only `\"""` is an escape), so a field name is found with or without an argument list. |
| API paths with dot segments or encoded separators | guide; the settings and delete checks now run on the resolved path too | The repo they reach is an `other_repo` question; what they do to it is not. |
| `rm -rf .git` or the checkout, `git clean -x`, force-deleting or force-moving a protected branch | keep | Cannot be undone (`clean -x` wipes ignored files such as `.env`). |
| `gh ruleset view`, `list`, `check` | remove | Reads. Any other `gh ruleset` verb stays blocked. |
| `gh issue transfer` | guide | It can be transferred back. |
| git and gh writes to a repo off the allowed list, or to an unnamed repo | guide | Not destructive, and the operator may ask for it in a session (an issue on the plugin's own repo, say). Runs keep to the project's repos through the CLI's write gate and `prohibitions.md`. |
| `gh project` reads and writes | guide writes, remove reads (changed by dblanken-yale/not-me#117) | Board changes are not destructive and the operator chose the board in setup. A board belongs to an owner, so no repo decides it. The CLI and skill prose use `gh api graphql` for boards; a run changes a board only through the CLI's write path, which logs it. |
| GraphQL mutations on the safe list, without the board marker | guide | Not destructive; everything else above still blocks. |
| Changing where git pushes: `git remote add\|set-url\|rename`, push config keys, `-c` keys on a read, file edits to `.git/config`, `.git/hooks/*`, `~/.gitconfig` | guide | Pushes are judged against the live config, so the protected-branch and force rules still hold after the change. |
| GitHub MCP tools | keep the hard rules only (deletes, settings, secrets; file writes, ref updates and branch merges on a protected or default branch; forced ref updates); remove the repo check and every other MCP tool | The operator decides which MCP tools to install and allow, and which repos to touch; what cannot be undone or skips review still blocks. Merging a pull request passes, as with `gh pr merge`. |
| Opaque text holding `push`, `reset --hard`, deletes, secrets, `branch -D`, `clean -x`, `rm -r .git` | keep for shell text, script files and code that starts a process or names git, gh, notme or the GitHub API | Reported under the first hard rule that is on; `other_repo` is no longer one of them. |
| The same words in interpreter code (`python3 -c`, `node -e`, a here-document fed to an interpreter) that starts no process and names none of those | guide (rule `process`) | Such code runs no git, gh or notme command; it only holds the word, for example in text it edits. `rm -r .git` and the allow list's name still block. |
| `allow-repo add`, `pause off`, `mode live` in opaque text | remove (dblanken-yale/not-me#118) | The not-me CLI is not judged any more; see the self-protection rows below. |

### Self-protection

| Item | Outcome | Why |
| --- | --- | --- |
| Reading or writing the operator token and the live-mode epoch; writing the live-mode marker | remove (dblanken-yale/not-me#118) | The token and the signed marker are gone; leftover files of those names are ordinary files. The read tools are no longer hooked. |
| Shell words naming the allow list (`allowed-repos.json`), and Write or Edit of it | keep (narrowed by dblanken-yale/not-me#118) | Changes go through `/not-me:allow-repo`, which shows them and logs them in the settings ledger. A recursive copy or search of the not-me home is no longer blocked: nothing there is secret. |
| A copy, move, link or extract into the not-me home (`cp`, `mv`, `ln`, `install`, `rsync`, `ditto`, `tar -C`, `unzip -d`), or a recursive one (`cp -r`, `rsync -a`, `ditto`, `mv`, `ln`, `tar`, `unzip`) into a directory above it; moving the home or a directory above it; `tar -P`; an extract with no `-C` or `-d` from the home or above it | keep (security review of dblanken-yale/not-me#118) | Each could replace the allow list without naming it. A copy out of the home, or a plain file copied into a directory above it, is allowed. |
| `notme allow-repo add\|remove`, `notme pause off`, `notme mode` other than show or dry-run, as parsed commands and in opaque text | remove (dblanken-yale/not-me#118) | A process rule now: `prohibitions.md` allows them only when the operator typed the slash command in that turn; the skills are user-invocable only. |
| A pseudo-terminal or detached wrapper around notme | remove (dblanken-yale/not-me#118) | It protected the CLI's terminal check for those commands, which is gone. Restricted mode still refuses another checkout and `NOT_ME_HOME` without a terminal or under a Claude Code ancestor. |
| A Write or Edit that leaves the global `mode` other than dry-run | keep (reworded by dblanken-yale/not-me#118) | `mode: live` in the global layer is enough now, so this sends the change through `/not-me:mode`, which shows and logs it. |
| A shell word or output redirection naming the global `project.md` (`echo >>`, `tee`, `sed -i`, `cp` or `mv` onto it), unless the program only reads it (`cat`, `grep`, `head`, a `sed` without `-i`, a copy out of it) | keep (security review of dblanken-yale/not-me#118) | The same change as the Write or Edit row, made from the shell. |
| Writes to the installed plugin's `hooks`, `bin`, `notme`, `cli` and `skills` | keep | Breaks the process: editing the guard switches every hard block off. |
| Writes to the global layer's `.md` files during a run | keep | The global layer can switch hard rules off (`confirm_guard_off`). |
| Writes to the repo layer `.claude/not-me/` during a run | guide (rule `process`) | The repo layer can only tighten, and a run edit there can wait for `/not-me:retro`. Blocking it also stopped other sessions in the same repo. |
| The hook fails closed on a crash when the input holds a risky word, and on a check slower than 8 seconds | keep | A broken guard must not let a hard-block action through. |

### CLI trust checks

| Check | Outcome | Why |
| --- | --- | --- |
| Restricted mode for `allow-repo add\|remove`, `pause off` and `mode live` (stdin a terminal, no Claude Code ancestor) | remove (dblanken-yale/not-me#118) | The operator typing the slash command is the approval; the skill runs the CLI in the session. |
| The operator token for allow-list and pause changes (`notme doctor --init-token`, `NOT_ME_OPERATOR_CONFIRM`) | remove (dblanken-yale/not-me#118) | Same. |
| The signed live-mode marker | remove (dblanken-yale/not-me#118) | `mode: live` in the global layer is enough; the repo layer still cannot set it. |
| Every allow-list, pause and mode change prints what changed and appends a line to `<not-me home>/settings-changes.jsonl` | keep (new in dblanken-yale/not-me#118) | The operator can see each change and undo it. |
| `--project-dir`, `CLAUDE_PROJECT_DIR` or `NOT_ME_HOME` naming another checkout refused in restricted mode | keep | A run acts on the checkout it runs in (decision 4). |
| `NOT_ME_DRY_RUN` can only turn dry-run on | keep | Safe direction only. |
| The write gate (`writes.check_write`): CLI writes only to origin, tracker, companions and the allow list | keep | Now the line that keeps runs to their project's repos, since the guard only guides there. |
| A tracker, companion or board named only in the repo layer counts when the same name is in that file on the default branch, without an allow-list entry: the commit `git ls-remote origin HEAD` reports, read from the local object store when it has been fetched | keep (new in dblanken-yale/not-me#118) | The file reaches the default branch only through a reviewed merge. A PR branch, a checked-out branch, the working tree or a moved local ref still cannot widen anything. The hook does not ask the remote; only the CLI write gate does. |
| The repo layer may only tighten (decision 1) | keep | A PR branch must not widen what not-me may touch. |
| `APPROVE` needs `review.auto_approve` in the global layer; `assign-issue` refused during a run; branch and worktree names checked | keep | Process rules the CLI can enforce exactly, with no false blocks. |

## The rule groups

There are four rule keys. Each is on by default. Each is switched by a key under `guard` in `project.md`.

| Rule | Kind | In one line |
| --- | --- | --- |
| `guard.protected_branches` | hard | No commits, merges, rebases, cherry-picks or hard resets on protected branches, and no pushes to them |
| `guard.force_push` | hard | No history rewrites and no deleting remote branches |
| `guard.destructive_git` | hard | No changing GitHub settings, secrets or rulesets, no deleting on GitHub, no `rm -rf .git`, `git clean -x` or forced deletes of a protected branch |
| `guard.other_repo` | guided | git and gh writes to repos off the allowed list, and changes to where git pushes, run and are logged |

A fifth rule, `process`, has no key: it is always guided. It covers edits to the repo layer config while a run is active.

Reads are never blocked. `gh pr view`, `git log`, `gh api repos/acme/lib/pulls` and similar work against any repo.

### other_repo (guided)

The allowed set is: the repo the checkout belongs to (`origin`), plus `tracker`, plus every repo in `repos`, plus `allow_repos` from your global layer, plus your allow list for this project and for all projects. A git or gh write to anything else runs and is logged as guided under `other_repo`, for example:

```
gh issue create --repo acme/not-me --title x --body-file b.md   # reporting a bug on the plugin's repo
gh pr comment 5 --repo acme/other --body hi
git push https://github.com/acme/lib.git HEAD
gh api -X POST repos/acme/lib/issues/1/comments -f body=x
GH_REPO=acme/lib gh pr create --title x --body y  # names no repo: gh's default repo is acme/lib
git remote set-url origin https://github.com/acme/lib.git
git config remote.origin.pushurl https://github.com/acme/lib.git
```

A gh command with no `--repo` (or `-R`) works on gh's default repo, and the guard works it out the way gh does (dblanken-yale/not-me#142): `GH_REPO` (on the command, set earlier in it, or in the environment), else the remote `gh repo set-default` marked (`remote.<name>.gh-resolved`), else the first GitHub remote of `upstream`, `github`, `origin` and the rest by name, read where the command runs (after a leading `cd`, see [Where git runs](#where-git-runs-git--c-a-leading-cd-and-variables)). So `gh issue comment 118 --body-file c.md` inside a checkout of an allowed repo just runs. A default it cannot work out (no GitHub remote, a `GH_REPO` the shell fills in, a directory it cannot name) is not blocked: the call runs and the guard log records it as guided, naming the repo it assumed (the checkout's `origin`). A `--repo` naming a repo off the allowed list is guided as before. `gh project` is never blocked and not judged by any repo, since a board belongs to an owner: a read (`list`, `view`, `item-list`, `field-list`) just runs and any other verb is logged as guided (dblanken-yale/not-me#117).

The process rule (`prohibitions.md`): a run writes only to the project's repos, through the `notme` CLI, whose write gate refuses anything else. In a session a person drives, an issue or comment on another repo is fine when that person asked for it, and always on the plugin's own repository. Setting `guard.other_repo: off` (with `confirm_guard_off`) stops the log lines and also lifts that write-gate check.

The protected-branch and force-push rules still apply to a push to any repo. A push whose target the guard cannot see (a `-c` push key, `GIT_DIR` or `--git-dir`, `send-pack`, `subtree push`, pushing submodules) is blocked under `protected_branches`, as is a write through a `--git-dir` or `--work-tree` path the shell fills in.

A git alias is looked up and judged as the command it stands for; a shell alias (`!...`) is judged as [opaque text](#opaque-text). A program the guard does not know that is handed `git` or `gh` as a word (`watch git push`, `flock x git push`) is judged as that git or gh command.

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
git -c remote.origin.push=x:refs/heads/main push
GIT_DIR=../other/.git git push                # also --git-dir, --work-tree, GIT_WORK_TREE, HOME
gh api -X PATCH repos/acme/app/git/refs/heads/main -f sha=abc
gh api -X PUT repos/acme/app/contents/x.md -f message=x -f content=eA==   # no branch: the default branch
```

#### Merging pull requests

The guard does not block merging a pull request: `gh pr merge`, the merge endpoints and any MCP merge tool pass. That is a policy, not a gap: no not-me skill merges, merging is not part of any skill's flow, and autonomous runs never merge (see the prohibitions every skill follows). A person merges, by hand or by asking Claude directly in a session. If you want GitHub to hold that line, require a pull request review on your protected branches (see [Honest limits](#honest-limits)).

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
gh api -X PATCH repos/acme/app/git/refs/heads/x -f sha=abc -F force=true
```

Allowed: a normal push that adds commits, such as `git push -u origin feature/x`.

### destructive_git

Shown in the block message as `destructive`.

Blocked, for example:

```
gh secret set TOKEN --repo acme/app --body x
gh variable set X --repo acme/app --body y
gh api -X PUT repos/acme/app/branches/main/protection --input p.json
gh api -X PUT repos/acme/x/../app/branches/main/protection --input p.json   # judged on the resolved path
gh release delete v1 --repo acme/app
gh repo edit acme/app --visibility public
gh repo delete acme/app --yes
gh issue delete 1 --repo acme/app --yes
gh api -X DELETE repos/acme/app/labels/x
gh api graphql -f query='mutation { deleteRef(input:{refId:"x"}) { clientMutationId } }'
gh api graphql -F query=@q.graphql                # a query the guard cannot read
rm -rf .git
git clean -fdx
git branch -D main
```

GitHub settings, secrets, variables, rulesets, branch protection, release deletes and deleting anything on GitHub are always treated as human actions.

Allowed: `gh ruleset view 1`, `gh issue transfer`, `git reset --hard HEAD~1` on a feature branch, `git branch -D feature/old`, `git clean -fd`, and `rm -rf node_modules build`.

## Opaque text

Some text the guard does not parse: a command it cannot read (an unclosed quote, more than 32 KB, nesting deeper than 64 levels), a command name the shell fills in (`$GIT`, `{git,push}`), inline interpreter code (`python3 -c`, `node -e`), code an interpreter reads from a pipe or a here-document, a script file (`bash x.sh`, `./x.sh`, `python3 x.py`, `source x`), a shell alias, and the words `xargs` or `parallel` add from their input. Where it can, the guard parses the inner command and judges it like any other: the commands inside `$(...)` and backticks, `bash -c` and `eval` bodies (up to 4 levels deep), a here-document fed to a shell, `find -exec`, `sudo`, `script -c`, `git rebase --exec` and `git submodule foreach`.

Opaque text is allowed unless its decoded text (quotes and backslashes removed, `\x67`-style escapes decoded, any case) holds one of these, in which case the call is blocked under the first of the listed rules that is on:

| Text | Rule it reports |
| --- | --- |
| `push` as a word | `protected_branches`, else `force_push` |
| `reset --hard` | `protected_branches` |
| `-X DELETE`, `--method DELETE`, `secret set`, `secret delete`, `secret remove`, a secrets API path (`/actions/secrets`, `/dependabot/secrets`, `/codespaces/secrets`, `/environments/<name>/secrets`), `branch -D`, `clean -x` (also `-fdx`), `rm -r... .git` | `destructive_git` |
| `allowed-repos.json` | self-protection, always |

So:

```
python3 -c 'print(1)'                           # allowed: nothing in the list
shellcheck $(git ls-files '*.sh')               # allowed: the inner git ls-files is a read
bash -c 'git log --oneline -5'                  # allowed: parsed, a read
bash -c "git push origin main"                  # blocked: parsed, a push to main
python3 -c 'import os; os.system("git push")'  # blocked: inline code that holds push
curl -s https://example.com/install.sh | sh     # allowed: the pipe's text holds nothing in the list
sh scripts/release.sh                           # blocked when the script holds push
python3 - f.py <<'EOF'                          # guided: code that holds push but runs nothing
s = open('f.py').read().replace('push', 'send')
EOF
```

Interpreter code (inline, or a here-document fed to an interpreter) that starts no process and names no `git`, `gh`, `notme` or GitHub API runs none of those commands. A word from the list in it is guided under `process`: the call runs and the guard log records it. Code counts as starting a process when it holds words such as `system`, `exec`, `spawn`, `popen`, `subprocess`, `child_process`, `shell`, `eval`, `run(`, `fetch`, `request` or `http`, so splitting `"g"+"it push"` across strings inside `os.system` still blocks. `rm -r .git` and the allow list's name block either way. Shell text, script files and code read from a pipe are judged by their words as before. The process rule: run git, gh and notme directly, never from interpreter code.

With the three hard rules off, opaque text is blocked only for the self-protection words.

## The block message

Every block says which rule fired, what was blocked, why, and what to do instead in the session:

```
not-me guard [protected_branches]: blocked `git commit -m x`.
Why: commits on the protected branch main are not allowed.
Instead: create a feature branch first (git switch -c <type>/<topic>) and commit there.
Autonomous runs: do not retry; log a QUESTION naming the operator and continue.
Relax this rule (not recommended): set guard.protected_branches: off and confirm_guard_off: [protected_branches] in ~/.claude/not-me/project.md
See effective config: /not-me:config
```

```
not-me guard [force_push]: blocked `git push --force origin feature/x`.
Why: force pushes rewrite published history.
Instead: push new commits on top instead; ask the operator if a rewrite is really needed.
Autonomous runs: do not retry; log a QUESTION naming the operator and continue.
Relax this rule (not recommended): set guard.force_push: off and confirm_guard_off: [force_push] in ~/.claude/not-me/project.md
See effective config: /not-me:config
```

No block message tells the operator to open a terminal or paste a plugin path; each says what to do next in the session.

A git write whose `--git-dir` or `--work-tree` path is a shell variable (`git --git-dir=$G commit ...`) is blocked: a literal `--git-dir` on a write is blocked too, so the Instead line says to drop the option and run `git -C /abs/path <sub> ...` with the path written out. Rewriting the command is the fix, so that message leaves out the "relax this rule" line:

```
not-me guard [protected_branches]: blocked `git --git-dir=$G commit -m x`.
Why: the guard does not expand shell variables in a `git --git-dir` path.
Instead: drop --git-dir and run `git -C /abs/path <sub> ...` with the path written out in full; a rewritten command is checked afresh, so this is not a retry.
Autonomous runs: do not retry; log a QUESTION naming the operator and continue.
See effective config: /not-me:config
```

### Where git runs: `git -C`, a leading `cd` and variables

Since dblanken-yale/not-me#144 a git write is judged on the branch checked out where it runs:

- `git -C <path>` names the checkout. A variable set to a literal path earlier in the same command is filled in: `W=/abs/wt; git -C "$W" commit ...` is judged in `/abs/wt`. That holds for plain assignments (and `export`) before any other command, until something may set the variable again (`read W`, `for W in`, `W=$(...)`, a prefix `W=x git ...`). A sourced script, `eval`, `trap`, a shell function, `set`, `declare`, `export` without a value, `read` and the other builtins that set a variable by name make every variable unknown.
- A leading `cd <path>` joined by `&&` is followed: `cd /abs/wt && git add x && git commit -F m` is judged in `/abs/wt`. Leading means the first command, after any such assignments. The cd counts only while the `&&` list lasts (pipes inside it included): after `;`, a newline, `||` or `&` the cd may have failed or run in the background, so later commands are judged where the session is. `cd /abs/wt; git commit` is judged where the session is.
- Any other `cd` (one after `;`, in a subshell, a group, a pipeline, after another command) is not followed: the write is judged where the session is, or where `git -C` points.
- A path the guard cannot work out (a variable it does not know, `$W/..`, a leading `cd` to a directory that does not exist, `cd -`, `cd` with options, a relative `cd` with `CDPATH` set, or any later `cd`, `pushd`, `popd`, `source`, `eval` or `trap` after a followed one) is not judged by the session's directory. The write runs and the guard log records it as guided under `protected_branches`. A checkout or switch there leaves every branch unknown for the rest of the command. A rule the command breaks on its own still blocks: a force push, a push to a protected branch it names, `clean -x`, a forced delete of a protected branch. A git alias there blocks, since the guard cannot look it up in a checkout it cannot name.

Skills run git in a run worktree as `git -C <worktree> ...`, never after `cd` (`scripts/check-skills.py` rule R16).

## Allow a repo

The allow list now matters to the `notme` CLI, whose write gate keeps runs to the project's repos; the guard only logs a write elsewhere. The tracker, companions and board named in `.claude/not-me/project.md` count without it once that file is on the default branch: the CLI asks the remote which commit its default branch is at (`git ls-remote origin HEAD`) and reads the file from that commit, so run `git fetch` after the merge. For anything else, or to use a new name before its pull request merges, you type `/not-me:allow-repo`; it runs the change in the session and prints what changed. A run never does it on its own.

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
- To remove an entry, type `/not-me:allow-repo --remove owner/name` (or `--remove --board owner/number`).
- Every add and remove prints what changed and appends a line to `~/.claude/not-me/settings-changes.jsonl`.
- If the file is not valid JSON, it is ignored, and only the checkout's own repo, `tracker`, `repos`, and global `allow_repos` are allowed. The `notme` CLI says so in its warnings.

`/not-me:setup` prints these commands for the companion repos and board it writes into the repo layer, for use before that file is merged.

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
- `other_repo: off` stops the guided log lines and lets the `notme` CLI write to any repo your token can reach.
- `protected_branches: off` allows commits and pushes on `main` and the other protected branches.
- `force_push: off` allows history rewrites and remote branch deletion.
- `destructive_git: off` allows changing repo settings and secrets, and deleting data.
- A block is usually a sign the run is doing something you did not intend.

## Self-protection

Self-protection is always on, in every directory, whether or not the guard is active there. It does not depend on the rule groups and cannot be switched off. It blocks:

- By file path: the allow list (`allowed-repos.json`). This covers Write and Edit, and Bash words and redirections that name the file after a path separator, resolve to it, or reach it through a glob or a part the shell fills in (`$VAR`, `$(...)`, backticks, `{a,b}`) when the rest of the path pins it there. A Bash word cannot tell a read from a write, so `cat` of the file blocks too; `/not-me:allow-repo --list` shows it. The search pattern of `grep`, `egrep`, `fgrep` or `rg` and a read-only `sed` script (no `-i`, no `w`, `r` or `e`) are not paths, so a pattern that only mentions the name is allowed. That holds only for a command line that is a single bare `grep`, `egrep`, `fgrep`, `rg` or `sed` command, with no assignments, substitutions (`$(...)`, backticks, `<(...)`) or words the shell fills in (`$VAR`, globs, `{a,b}`), no `GREP_OPTIONS`, `RIPGREP_CONFIG_PATH`, `BASH_ENV` or exported function in the hook's environment, and whose program resolves on `PATH` to a file the user cannot write, in a directory the user cannot write, with no relative `PATH` entry before it (never on Windows). Otherwise, or when an option is unknown and the pattern cannot be told apart from a file, every word is checked. A shell function or alias defined in your own shell startup files is outside what the guard sees.
- A Write or Edit that would leave `mode: live` (or anything but `dry-run`) in your global `project.md`, and any shell write of that file (a redirection, `tee`, `sed -i`, `cp` or `mv` onto it). `mode: live` there is enough to go live, so the change goes through `/not-me:mode live`, which shows it and logs it. Reading the file (`cat`, `grep`, a `sed` without `-i`, a copy out of it) is allowed.
- A copy, move, link or extract into the not-me home, or a recursive copy, a move, a link or an extract into a directory above it (`cp -r`, `rsync -a`, `ditto`, `mv`, `ln`, `tar -C`, `unzip -d`, and `tar` or `unzip` with no destination when the working directory is the home or above it); moving the home or a directory above it; and `tar -P`, which extracts to absolute paths. Each could replace the allow list without naming it.
- Writes to the installed plugin's own files: its `hooks`, `bin`, `notme`, `cli`, and `skills` folders.
- Writes to the global layer's `.md` files while a run is in progress. A write to the repo's `.claude/not-me/` folder during a run is guided under `process`, not blocked: the repo layer can only tighten.

The not-me CLI itself is not judged (dblanken-yale/not-me#118). `/not-me:allow-repo`, `/not-me:pause` and `/not-me:mode` are user-invocable only, so you typing one is the approval, and the skill runs `notme allow-repo add|remove`, `notme pause off` or `notme mode live` in the session. [`references/prohibitions.md`](../plugins/not-me/references/prohibitions.md) forbids a run from issuing them unless you invoked the matching slash command in that turn; an autonomous run logs a QUESTION instead. Every such change prints what changed and appends a line to `~/.claude/not-me/settings-changes.jsonl`. Files an older install left behind (`operator-token`, `mode-live.sig`, `mode-live.epoch`) do nothing now; you may delete them.

The block message in each case says to ask the operator, which is you.

## When the guard itself breaks

- If the hook crashes, it reads its raw input as [opaque text](#opaque-text): input that holds one of those words is blocked with `not-me guard failed closed: <error>; run /not-me:doctor`, anything else is let through.
- A check that runs longer than 8 seconds blocks the call, so a slow check cannot run into the hook's 10 second timeout, which would let the call through.

A missing Python is the case the hook cannot report itself. The launcher exits with code 7, and Claude Code treats any exit code other than 2 as a non-blocking error, so the call would run unchecked. To catch this, every autonomous run starts with a self-test. It pipes a command that must always be blocked (a write to the allow list) through the real hook command and checks that the exit code is 2. If it is not, the run stops with `FAILED - guard not working`. `/not-me:doctor` runs the same test.

## Honest limits

The guard splits a command into words the way the shell does: quotes, backslashes, line continuations, `$(...)`, backticks, here-documents and redirections. It does not run anything. It fills in only variables set to a literal path in the same command and follows only a leading `cd`, and only for git and gh (see [Where git runs](#where-git-runs-git--c-a-leading-cd-and-variables)). It is a net for an agent that follows the rules, kept small on purpose: an earlier version tried to prove every command safe and blocked so much ordinary work that not-me could not develop itself. The trade-off is that a determined or buggy program can get around it.

What it does not cover:

- `cd` past the leading one. `true && cd ../other && git push` is judged as a push from the session's checkout, and a write where the guard cannot work out the checkout runs and is only logged. Name the checkout with `git -C` instead.
- Programs it does not read. A script file is scanned only for the opaque-text words, the modules a script imports and the file arguments of `python3 -m` are not read, a file written and run in the same command is read before the write, and a script that builds its command from pieces will not match. Programs that run commands from their own arguments (`awk 'BEGIN{system(...)}'`, `sed e`, `vim -c '!...'`, `tar --to-command`, `less +!`) are not inspected.
- Merges. The guard does not block them; the skills do not merge. See [Merging pull requests](#merging-pull-requests).
- Writes to other repos. They are guided, not blocked: a run that ignores `prohibitions.md` and calls `gh` directly can write to any repo your token can reach. The CLI's write gate holds only for writes through `notme`, and the guard log shows the rest.
- The default-branch check of the repo layer asks the `origin` remote which commit its default branch is at, so moving a local ref does not count. What remains trusted is the `origin` URL itself: a session that pointed `origin` at a repository it controls could make a name count. Changing where git pushes (remote URLs, `insteadOf`) is guided and logged, not blocked. The guard hook does not ask the remote at all; it only guides those writes.
- A `cd` into the not-me home followed by a write with a relative path (`cd ~/.claude/not-me && echo x >> project.md`) is outside the path checks, since the path checks do not follow `cd`.
- `tar` and `unzip` option parsing is best effort; unusual option forms may not be recognised.
- The path guard follows literal paths and the working directory it tracks, not every program that can write a file: an editor, a pager's log option, a build tool or a script that opens the allow list or the global `project.md` itself is outside its view.
- The operator commands. `notme allow-repo add`, `pause off` and `mode live` are not blocked; only `prohibitions.md` keeps a run from issuing them on its own. The settings ledger shows each change after the fact.
- Pushes and writes through other tools. `curl` or any HTTP client to the GitHub API, a gh extension or alias, a git hook, an external `git-<name>` program, MCP tools other than GitHub ones, the repo a GitHub MCP tool writes to, and your own terminal are outside its view.
- Reads. Any repo your token can read can be read.
- Time of check. It looks at your git config when it checks. Something that changes the config between the check and the command is out of reach.
- Git config that runs a command: `core.pager`, `core.editor`, `core.sshCommand`, `diff.*.textconv`, `core.hooksPath`, `GIT_PAGER` and the like, set with `-c` or in the environment, are out of scope; the session can run any command anyway, and GitHub rulesets are the backstop.

Use the guard with the protections that do not depend on it:

- On GitHub, add a ruleset (or branch protection) to every protected branch of every repo not-me works in: restrict deletions, block force pushes, and require a pull request (with a review, if you want a person on every merge) before merging. That is the control a script cannot route around.
- A token with the narrowest scopes that work.
- A person who decides what merges.
