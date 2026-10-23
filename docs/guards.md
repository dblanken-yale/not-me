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
perl -e 'system("g"."it push https://gith"."ub.com/acme/other HEAD")'
vim -c 'exe "!g"."it push https://gith"."ub.com/acme/other HEAD"'
```

Interpreters (python, node, deno, bun, perl, ruby, php, lua, Rscript, osascript, pwsh and powershell, julia, tclsh, also with a version in the name such as `python3.12`) are allowed only in these shapes:

- `<interpreter> [safe options] <existing script file> [arguments]`. The safe options cannot load code: python `-u`, `-B`, `-O`, `-OO`, `-W ignore|error|default`; node `--no-warnings`; perl `-w`, `-W`, `-X`, `-T`; ruby `-w`; none for the others. pwsh and powershell: only `-NoProfile -NonInteractive -File <file> [arguments]`.
- `python3 -m <module> [arguments]` for a short list of tool modules: `pytest`, `pip`, `venv`, `json.tool`, `unittest`, `http.server`, `py_compile`, `compileall` and similar. `timeit`, `pdb`, `cProfile`, `trace`, `code` and `runpy` run code taken from their arguments and are not on it.
- `<interpreter> --version`, `-V` or `--help`.

The script file an allowed call runs is read before it runs, and is blocked (self-protection, in every directory) when it can forge a terminal or detach, or names the operator token, the live-mode files, the allow list or the merge-grant folder. One parser decides both which shape a call has and which file that is, so the two cannot disagree. The file arguments of a `python3 -m` call (`python3 -m pytest tests/x_test.py`) are read the same way; the modules those files import and a `conftest.py` are not, which is the same limit as running a file. A command that writes a file (`>`, `>>`, `tee`, `cp`, `mv`, `install`, `ln`, `dd of=`, `sed -i`, `curl -o`, `wget`, a `cat <<EOF >` here-document) and also runs an interpreter on a file is blocked, because the guard reads the file before the write happens: write in one Bash call and run in the next. The interpreter's own output redirection (`python3 x.py > out.txt`) does not count.

Everything else is blocked, whatever the code holds: inline code (`-c`, `-e`, `--eval`, attached forms, `deno eval`), no script file (code from stdin, a pipe, a here-document, `-`, `/dev/stdin`, `-i`, a prompt), any other option (`-M`, `-r`, `--require`, `-d`, `-I`, `php -d`), a script path that is not an existing regular file, and any environment assignment or `env` wrapper in front of the interpreter (also a variable such as `PYTHONSTARTUP`, `NODE_OPTIONS`, `PERL5OPT` or `LUA_INIT` exported earlier in the command). The guard cannot tell what a program does from its text. Write the code to a script file and run `<interpreter> file`; `npm test` is unaffected. In vim, `exe`, `execute`, `call system(...)` and similar are blocked.

Edits to `.git/config`, `.git/hooks/*`, and `~/.gitconfig` are blocked too, because they change where git sends things.

A git or gh call counts as a read only when the guard can prove it is one. An unknown git subcommand or alias, an option before the subcommand other than `-C`, an option on a read that is not on its allow list (`--ext-diff`, `--output`, `grep -O`, `ls-remote -u`), an option that runs a program (`rebase --exec`, `clone --template`, `--upload-pack`, a custom merge strategy, `push --recurse-submodules`), and any `git -c` key or `git config` write outside a short safe list (`user.name`, `user.email`, `color.*` and similar) are blocked. So are abbreviated long options (git accepts `--exe` for `--exec`), setting a variable that can make git or gh run a program or read other config by any means (`GIT_*`, `GH_*`, `EDITOR`, `PATH`, `HOME`, `XDG_CONFIG_HOME`, `LD_PRELOAD`, `IFS`, a `PAGER` other than `cat` or `less`), gh or git named anywhere in the arguments of a program that can run commands (`xargs git`, `awk 'BEGIN{system("git ...")}'`, `vim -c '!git ...'`), a graphql query that is not a literal, and REST endpoints with `..` or encoded separators. A `git push` with no branch named is judged by `remote.<name>.push`, `push.default` and the branch's upstream; it is blocked when that reaches a protected branch or cannot be told.

#### Directory changes and writes

The guard does not follow `cd`. It judges a git write in the working directory Claude Code reports for the call, or in the `git -C <path>` it names, and a gh write by its `-R owner/name`. So a single Bash command that changes directory anywhere may not also write with git or gh:

```
cd ../app-ui && git commit -F msg.txt           # blocked: cd and a commit in one command
(cd sub && git push)                            # blocked: a subshell cd counts too
env -C ../other git push                        # blocked
git -C ../app-ui commit -F msg.txt              # allowed: the path is named
gh pr create -R acme/app --title x --body y     # allowed
cd sub && make test                             # allowed: no write
cd sub; git status                              # allowed: a read
```

What counts as changing directory: `cd`, `pushd`, `popd`, `chdir`; `source` and `.`; `eval`, `exec`, `trap`, `alias`, and function definitions; `env -C` (also grouped, as in `-iC`), `sudo -D`, and `find -execdir`; a `cd` behind `xargs`, `nohup`, `env`, `command` or `builtin`; setting or declaring `PWD`, `OLDPWD`, `CDPATH`, `HOME` or `DIRSTACK` (including `declare -n` and computed names); and a directory named on its own, which zsh's `auto_cd` enters. What counts as a write: every git subcommand except plain reads (`status`, `log`, `diff`, `show`, `rev-parse`, and listing forms such as `git branch -a`), and every gh command the rules above treat as a write. A command that changes directory also cannot run `notme`, a relative script, or a relative read, copy or `rm -r` that could reach the operator token or the checkout. Run the `cd` in its own Bash call first if you need it.

Allowed, for example:

```
gh pr view 5 --repo acme/other                  # reads work anywhere
gh api repos/acme/lib/pulls
gh pr comment 5 --repo acme/app --body hi       # the checkout's own repo
git push -u origin feature
python3 scripts/report.py                       # a script file: run, not opened
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

`gh pr merge` and the merge endpoint (`gh api -X PUT repos/acme/app/pulls/1/merge`) are blocked whatever the branch, because a person merges. You can hand Claude one merge with a merge grant, below.

#### Merge grants

To let Claude merge one PR for you, type this as your whole message:

```
/not-me:allow-merge acme/app#12
/not-me:allow-merge acme/app#12 --rebase     # or --merge; the default is --squash
```

Claude then runs `gh pr merge 12 -R acme/app --squash` once, and the guard lets exactly that through. How it works:

- Only you can create a grant. A `UserPromptSubmit` hook reads what you submit and nothing Claude does. When your message is exactly `/not-me:allow-merge owner/name#N` (optionally `--squash`, `--merge` or `--rebase`, nothing else, one line), it writes a grant for this session to `~/.claude/not-me/merge-grants/<session>.json`. The command is marked `disable-model-invocation`, so Claude cannot run it through the Skill tool. A malformed argument, or the command anywhere but the start of the message, grants nothing.
- A grant is signed with your [operator token](install.md#the-operator-token), the same secret `/not-me:mode live` signs with, and the guard checks the signature. A grant file written any other way does not count, and without a token no grant is made (the hook says so; create one with `notme doctor --init-token` in your own terminal).
- A grant lasts until your next message. Any other message in the session deletes it, and it expires after 30 minutes regardless.
- It covers one merge. The guard deletes the grant when it allows the merge, and logs the use in the guard log, `<git-common-dir>/not-me/logs/guard.jsonl` (`rule: merge_grant`, the PR and the session).
- It covers one exact command: `gh pr merge <N> -R <owner/name>` (or `--repo`), the method flag, and optionally `--subject` and `--body-file` once each. The method is part of the grant: a grant that names `--merge` or `--rebase` allows only that flag, and one that names none allows only `--squash`. The number and repo must match the grant (the repo ignoring case), and the repo must be on the allowed list. `--admin`, `--auto`, `--delete-branch`, the merge endpoint, a merge without an explicit number or `-R`, a PR URL, a variable, a substitution, a redirection, a comment and any command joined with `;`, `&&`, `|` or a newline stay blocked.
- It belongs to one session. A grant written in another Claude Code session does not apply.
- Runs never merge. While a not-me run is active in the checkout, a grant does not apply.
- Nothing but the hook may touch a grant: the Read, Grep, Glob, LS, Write and Edit tools and any Bash command that names the `merge-grants` folder or reaches it through a glob, a link or a redirection are blocked, in every directory. See [Self-protection](#self-protection).

Outside a not-me project (no `.claude/not-me/` folder and no run) the four rules are off, so `gh pr merge` is not blocked there and a grant is neither needed nor spent. The grant folder is still protected there.

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
- `protected_branches: off` also stops blocking `gh pr merge`, so a run could merge. To merge one PR, use a [merge grant](#merge-grants) instead.
- `force_push: off` allows history rewrites and remote branch deletion.
- `destructive_git: off` allows changing repo settings and secrets, and deleting data.
- When a command is too opaque to check (`bash -c ...`, `$(...)`), the guard reports it under the first rule that is still on. With all four off, those commands are allowed.
- A block is usually a sign the run is doing something you did not intend. Allowing one repo is nearly always the better fix.

## Self-protection

Self-protection is always on, in every directory, whether or not the guard is active there. It does not depend on the four rules and cannot be switched off. It blocks:

- Any Write or Edit of `allowed-repos.json`, and any Bash command that mentions that file or its full path.
- Any Bash command containing `allow-repo` together with `add`, `remove`, or `--everywhere`, for example `notme allow-repo add acme/other`.
- Turning a pause off (`notme pause off`), and driving the not-me CLI through a pseudo-terminal or a detached process.
- Writes to the plugin's own code: its `hooks`, `bin`, `notme`, `cli`, and `skills` folders.
- Writes to not-me config while a run is in progress: the repo's `.claude/not-me/` folder and the `.md` files in your global folder.
- Reading or changing the operator token or the live-mode epoch (`mode-live.epoch`). This covers the Read, Grep, Glob and LS tools, Write and Edit, and Bash commands that name either file or reach it through a glob (`~/.claude/n*me/mode-live.e*`), a directory, a redirection, or a part the guard cannot work out, such as `$VAR`, `$(...)`, backticks or `{a,b}`, when the rest of the path could lead there.
- Going live: `notme mode` with anything but `show` or `dry-run`, however it is started (`notme`, `bin/notme`, `python3 cli/notme.py`, `python3 -m notme.cli`, also after python options such as `-W ignore` or `-X utf8`). Also any Write or Edit of `mode-live.sig`, any Bash command that mentions it, and any Bash copy, move or redirection that reaches it through a glob or a part the guard cannot work out. That file is the signature `/not-me:mode live` writes with your operator token and epoch; without it a `mode: live` setting runs as dry-run, so no file edit can turn live mode on. Going back to dry-run with `/not-me:mode dry-run --global` replaces the epoch, so a saved copy of an old signature stops working.
- A Write or Edit that would leave `mode: live` (or anything but `dry-run`) in your global `project.md`. This one is a courtesy: it stops the attempt early with a pointer to `/not-me:mode live`. The signature above is what actually keeps live mode off.
- Creating, changing or reading a merge grant: anything under `~/.claude/not-me/merge-grants/`. This covers the Read, Grep, Glob and LS tools, Write and Edit (also through a symlink, a `..` step after a symlink, or a hard link to a grant), and any Bash command that names the folder or reaches it through a glob, a directory or a redirection. Only your own `/not-me:allow-merge` message creates one (see [Merge grants](#merge-grants)).

The block message in each case says to ask the operator, which is you.

## When the guard itself breaks

The guard fails closed:

- If the hook crashes on a call that mentions `gh`, `git`, GitHub, `.git/`, or the allow list, it blocks the call. The message is `not-me guard failed closed: <error>; run /not-me:doctor`. A crash on a call that has nothing to do with git or GitHub lets the call through.
- Anything the hook cannot read well enough to check is blocked, not guessed at. A Bash command longer than 32 KB, nested more than 64 levels deep, or with quoting it cannot read is blocked unread, whatever rules are on.
- A check that runs longer than 8 seconds blocks the call, so a slow check cannot run into the hook's 10 second timeout, which would let the call through.

A missing Python is the case the hook cannot report itself. The launcher exits with code 7, and Claude Code treats any exit code other than 2 as a non-blocking error, so the call would run unchecked. To catch this, every autonomous run starts with a self-test. It pipes a command that must always be blocked (a write to the allow list) through the real hook command and checks that the exit code is 2. If it is not, the run stops with `FAILED - guard not working`. `/not-me:doctor` runs the same test.

## Honest limits

The guard splits a command into words the way the shell does: quotes, backslashes, line continuations, `$(...)`, backticks, here-documents and redirections. It does not run anything, expand variables or follow `cd`. It understands `git`, `gh`, `rm`, `find`, `xargs`, `sudo`, `env`, `time`, `timeout`, and a few more. It is a net for an agent that follows the rules. A determined or buggy program can get around it.

What it does not cover:

- Programs and scripts it cannot read. A shell script is scanned for `git`, `gh`, and `github.com`, but a file run as `python3 tool.py` is not opened, and a script that builds its command from pieces will not match. Running a file is the limit of text inspection: whatever a script file does is out of reach, which is why inline interpreter code, which could be any program, is blocked outright. A file such a program writes into the merge-grant folder is not signed with your operator token, so it does not count as a grant.
- Tools outside its matcher. Only Bash, the file-editing tools, and MCP tools with `github` in the name are checked. Other MCP servers and your own terminal are not.
- Reads. Any repo your token can read can be read.
- Time of check. It looks at your git config when it checks. Something that changes the config between the check and the command is out of reach.
- The repo layer's `tracker` and `repos`. The hook allows the repos named there without looking at your allow list, while the `notme` CLI requires you to confirm them. Review changes to those two keys the way you would review code.

The cost of its caution is false alarms: it blocks constructs it cannot see through, even when they are harmless. When that happens, run the `git` or `gh` command directly, without a wrapper, and it will be checked normally.

Use the guard with the usual protections: a token with the narrowest scopes that work, branch protection on the repos that matter, and a person who merges.
