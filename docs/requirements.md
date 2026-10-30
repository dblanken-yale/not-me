# Requirements

What not-me needs on your machine, what it asks of GitHub, what it asks of Claude Code, and every place it reads or writes on disk. Read this before you install if you need to get the plugin approved by someone else.

not-me is a Claude Code plugin. It picks up GitHub issues, opens pull requests, reworks them after review, reviews them, and proves them on a test site. It does the GitHub work through a small command line tool called `notme` (stdlib-only Python) and a hook that checks every risky command first. See [guards.md](guards.md) for the hook.

## CLI tools it needs and how it uses each

| Tool | Minimum | Required | What not-me does with it |
| --- | --- | --- | --- |
| `git` | 2.30 | yes | Local branch and commit work, merging, and pushing feature branches |
| `gh` (GitHub CLI) | 2.40 | yes | Every GitHub read and write |
| `python3` | 3.9 | yes | Runs the `notme` CLI and the guard hook. Standard library only, nothing to `pip install` |
| `bash` | any | yes | Starts the Python launcher. On Windows it comes with Git for Windows |
| `playwright-cli` | any | no | Browser steps for `/not-me:prove` and visual checks. Without it, visual checks are logged as not run |

### git

not-me runs these git operations and nothing else:

- Read state: `branch --show-current`, `status --porcelain`, `remote get-url`, `symbolic-ref` (to find the default branch), `rev-parse`, `config --get`.
- `fetch` of named refs from `origin`.
- `merge --ff --no-edit <base>` to bring a base branch into a feature branch. It never rebases. If the merge stops on conflicts, not-me reports that and carries on from there.
- `checkout --no-track -b <branch> <base>` to start a branch.
- `stash push --include-untracked` and `stash apply` by commit id, to keep your uncommitted work safe while it switches branches.
- `worktree add --detach` and `worktree remove` for review runs, which use a separate worktree by default.
- `push --dry-run` to a throwaway ref name, to test that a push would work. Nothing is sent.
- `push -- origin refs/heads/<name>:refs/heads/<name>`.

Push rules, all enforced in `notme/git.py` and again by the guard hook:

- There is no force flag anywhere in the code. A push with `--force`, `-f`, `--force-with-lease`, or a `+` refspec is blocked by the hook.
- The push uses an explicit refspec, so a `remote.origin.push` setting cannot send it somewhere else.
- It refuses to push a protected branch (default `main`, `master`, `develop`).
- Every push URL of `origin` must point at one GitHub repo that not-me is allowed to write to.
- Branch names are checked: no leading dash or `+`, no `:`, no spaces, no `..`.

### gh

The `notme` client sorts every `gh` call into a read or a write. A call is a read only if it is on this list:

| Allowed read | Notes |
| --- | --- |
| `gh issue view`, `gh issue list` | |
| `gh pr view`, `gh pr list`, `gh pr diff`, `gh pr checks` | |
| `gh repo view` | |
| `gh run view`, `gh run list` | |
| `gh label list` | |
| `gh search ...`, `gh status` | any arguments |
| `gh api <endpoint>` | GET only. No `-f`, `-F`, `--field`, `--raw-field`, `--input`, and no method override other than GET |
| `gh api graphql` | Only `-f key=value` pairs, one inline `query=`, no word `mutation`, no `@file` |

Everything else counts as a write and goes through the write gate (see [guards.md](guards.md)). A second list is refused outright, as a read or a write: `gh auth`, `gh config`, `gh alias`, `gh extension`, `gh secret`, `gh variable`, `gh gist`, `gh ssh-key`, `gh gpg-key`.

Text for comments, reviews, and PR bodies is never put on the command line. It goes in a file, and the file must be inside the project or inside not-me's private temp directory. Files named like secrets (`.env*`, `*.pem`, `*.key`, `id_*`, `.npmrc`, `.netrc`, `auth.json`, `credentials*`), symlinks, files with more than one hard link, and anything inside `.git/` are refused.

### python3

Python 3.9 or newer, standard library only. The launcher `bin/notme-python` tries `python3`, then `python`, then `py -3`. It test-runs each one, which skips the Windows Store stub and any Python older than 3.9. If none works it prints how to install one and exits with code 7. See [install.md](install.md).

### playwright-cli (optional)

Used only to drive a browser for `/not-me:prove` and for visual checks on UI changes. A visual check that cannot find it logs `Visual check: not run (browser tool missing)` and carries on. `/not-me:prove` without it posts nothing and ends `FAILED <ref> no browser tool`.

### bash on Windows

Claude Code on Windows runs shell commands through Git Bash, and the hook command starts with `bash`. Install Git for Windows. The launcher converts absolute POSIX path arguments to Windows form when it runs under Git Bash.

## GitHub permissions it needs and uses

### Token scopes

not-me uses the login that `gh` already has. It never asks for a token and never stores one. Check the login with `gh auth status`.

| Scope | Needed for |
| --- | --- |
| `repo` | Issues, pull requests, labels, comments, reviews, and pushing branches |
| `read:org` | Organisation and team lookups, such as asking a team to review. `gh` asks for it by default |
| `project` | Only when `queue.source` is `board` or `either` (a Projects v2 board). Add it with `gh auth refresh -s project` |

`gh auth login` grants `repo` and `read:org` by default. If your organisation uses SAML single sign-on, authorise the token for that organisation too.

### What it writes

Each of these goes through the write gate, which allows the repo the checkout belongs to, the tracker and companion repos, and anything on your allow list:

- Branches and commits, pushed to the allowed repo, on feature branches only.
- Pull requests: create, edit the body, request reviewers, set labels, assign.
- Comments on issues and pull requests: create, and edit the comments it wrote earlier. Replies on review threads. Resolving review threads that a review run confirmed as fixed.
- Pull request reviews with the event `COMMENT` or `REQUEST_CHANGES`. An `APPROVE` review is refused unless a skill explicitly asks for it.
- Issue labels: always the whole label set in one write, then a read-back to check it. Never an add and a remove in one command.
- Issue assignees: it assigns the operator when it claims an issue and removes the operator when it releases one.
- Issue bodies, when you ask it to groom a ticket.
- Board status: one field on one item of the one configured board, through a GraphQL mutation that only the CLI sends.

### What it never does

- Merge a pull request. A person merges. The hook blocks `gh pr merge` and the merge endpoint.
- Force-push, rewrite published history, or delete a remote branch.
- Delete or rename a branch, tag, release, label, or repository.
- Change repository settings, branch protection, secrets, variables, or workflow permissions.
- Write to any repo that is not on the allowed list.
- Post credentials, tokens, or the contents of `.env` files.

The full list is in `plugins/not-me/references/prohibitions.md`. No config setting and no comment from anyone lifts it.

## Claude Code permissions

### Tools the skills use

- Bash, for `notme`, `git`, `gh`, and `playwright-cli`.
- Read, Write, Edit, and MultiEdit, for code in the checkout and for body files in the private temp directory.
- Subagents (the Agent tool), to hand units of work to implementers and reviewers. The `models.*` keys choose the model for each. A `--bg` run needs `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` of at least 3, and `/not-me:doctor` checks it.
- Optional: the `pr-review-toolkit` plugin's `code-reviewer` agent, for the gate's code review step. Without it, a general-purpose subagent runs the review (see `install.md`).
- Any GitHub MCP tool you have installed, which the hook also covers (below).

### What the PreToolUse hook intercepts

One hook, from `hooks/hooks.json`, with the matcher `Bash|Write|Edit|MultiEdit|NotebookEdit|Read|NotebookRead|Grep|Glob|LS` and a 10 second timeout. It runs before the tool does. MCP tools are not judged.

- Exit code 0: the tool call goes ahead.
- Exit code 2: the tool call does not run. The message on stderr goes back to Claude, which reads why it was blocked and what to do instead.

Ordinary commands that mention neither `git` nor `gh` cost tens of milliseconds. The hook is explained in [guards.md](guards.md).

### Permission modes

- Interactive CLI and desktop: Claude Code's own permission prompts still apply. The hook is an extra layer on top. It does not approve anything for you.
- Desktop auto mode: the hook still runs on every matching call, and a block still stops the call and returns its message to Claude. Auto mode does not skip hooks.
- Headless and eval runs (`claude -p`, `--bg`, CI): nobody can answer a prompt, so a tool that is not pre-approved is simply denied. Pass the tools you need with the CLI's `--allowedTools` option (Bash, Read, Write, Edit, MultiEdit, and Agent). The hook still applies.

## Filesystem

| Path | Read or write | Written by | Committed? |
| --- | --- | --- | --- |
| `.claude/not-me/*.md` in the repo's main checkout (the repo layer) | read, and written by setup | `/not-me:setup`, or you | Yes. This is the only thing not-me asks you to commit |
| `.claude/not-me/PAUSED` | read | you (an empty file) | No. Keep it out of version control |
| `~/.claude/not-me/*.md` (the global layer; set `NOT_ME_HOME` to move it) | read, and written by setup | `/not-me:setup --global`, or you | No, it is outside any repo |
| `~/.claude/not-me/PAUSED` | read | you | No |
| `~/.claude/not-me/allowed-repos.json` | read by the hook and CLI, written by `/not-me:allow-repo` only | the operator, never the agent | No |
| `<git-common-dir>/not-me/claims/issue-<n>.lock` | read and write | `notme`, to stop two sessions on one machine taking the same issue | No |
| `<git-common-dir>/not-me/active-run` | read and write | `notme`, a small JSON marker while a run is going | No |
| `<git-common-dir>/not-me/logs/<skill>/<timestamp>-<ref>-<id>.md` | write | `notme`, one run log per run | No |
| `<git-common-dir>/not-me/logs/intended-writes.jsonl` and `logs/payloads/<sha>.md` | write | `notme`, the dry-run ledger | No |
| `<git-common-dir>/not-me/logs/guard.jsonl` | write | the hook, one line per block or guided call with its input, redacted and capped | No |
| `<main checkout>/.claude/worktrees/<name>/` | write, and removed after each PR | `notme git worktree-add` and `worktree-remove`, one detached worktree per PR a review checks out | No |
| `<git-common-dir>/info/exclude` | one line appended, `/.claude/worktrees/`, once | `notme git worktree-add`, so review worktrees never show as untracked | No, it is git's local ignore file |
| `not-me-<uid>/` under `$XDG_RUNTIME_DIR` or the system temp directory (mode 0700) | write | `notme`, body files and JSON payloads | No |
| `${CLAUDE_PLUGIN_ROOT}` (the installed plugin) | read | Claude Code, on install and update | No |
| `${CLAUDE_PLUGIN_DATA}` | none today | reserved | No |

Notes:

- `<git-common-dir>` is the shared `.git` directory of the checkout, which is `.git` in a normal clone. Git never tracks anything inside it, so logs and locks cannot be committed by accident.
- Outside a git repository the logs go to `.not-me/logs` in the project instead. Keep that directory out of version control.
- `NOT_ME_LOG_ROOT` or the `logs.root` key can move the log directory. The hook looks for the active-run marker in `<git-common-dir>/not-me/active-run` only, so leave the log root alone unless you have a reason.
- The private temp directory is created with mode 0700. not-me refuses to use it if it is a symlink, belongs to someone else, or has a different mode.
- Run logs, the ledger, and the guard log are local working records. They are not uploaded anywhere.
- The repo layer is read only from the main checkout. A pull request head, a review worktree, or a fork that contains its own `.claude/not-me/` is data, never config.
