# Install

This page takes you from nothing to a first dry run. A dry run reads from GitHub for real but only records the writes it would have made. Nothing is posted until you switch to live mode.

You need four things: `git` 2.30 or newer, `gh` 2.40 or newer, `python3` 3.9 or newer, and a Claude Code client. See [requirements.md](requirements.md) for what each is used for.

## 1. Install the tools

Pick your operating system. Then check the versions at the end of the section.

### macOS

```bash
brew install git gh python
```

### Debian and Ubuntu

`git` and `python3` come from the distribution. `gh` comes from the official GitHub CLI apt repository, because the distribution package is often too old.

```bash
sudo apt update
sudo apt install -y git python3 curl

sudo mkdir -p -m 755 /etc/apt/keyrings
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
  | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null
sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt update
sudo apt install -y gh
```

Ubuntu 20.04 ships `git` 2.25 and Python 3.8, which are both too old. Use a newer release, or install newer versions yourself.

### Fedora

```bash
sudo dnf install -y git gh python3
```

### Windows

Install Git for Windows. It provides Git Bash, which Claude Code and the not-me hook both need.

With winget:

```powershell
winget install --id Git.Git -e
winget install --id GitHub.cli -e
winget install --id Python.Python.3.12 -e
```

With scoop:

```powershell
scoop install git gh python
```

Close and reopen your terminal afterwards so the new `PATH` is picked up.

The Python Store stub: a fresh Windows install has a fake `python3.exe` in `WindowsApps` that opens the Microsoft Store instead of running Python. The not-me launcher test-runs every candidate (`python3`, `python`, then `py -3`) and skips the stub. If the launcher still reports no Python, install Python from python.org or with winget as above, tick "Add python.exe to PATH" in the installer, and restart Claude Code. Do not install it from the Store.

### Check the versions

```bash
git --version
gh --version
python3 --version      # on Windows: py -3 --version
```

## 2. Sign in to GitHub

```bash
gh auth login
```

Choose GitHub.com, HTTPS, and the browser flow. The default scopes include `repo` and `read:org`, which is enough for label queues. Check with:

```bash
gh auth status
```

If your queue is a Projects v2 board (`queue.source: board`), add the `project` scope:

```bash
gh auth refresh -s project
```

If your organisation uses SAML single sign-on, authorise the token for it on GitHub.

## 3. Install the plugin in your client

Both clients use the same two commands. Run them inside Claude Code.

### Claude Code CLI

Start `claude` in any directory, then:

```
/plugin marketplace add dblanken-yale/not-me
/plugin install not-me@not-me
```

You can do the same from a normal terminal:

```bash
claude plugin marketplace add dblanken-yale/not-me
claude plugin install not-me@not-me
```

Restart the session if Claude Code asks you to.

### Claude Code desktop

Open a session in the Code tab and type the same two slash commands. The plugin, its hook, and its skills work the same way as in the CLI.

Auto mode is supported. The guard hook runs before every matching tool call in every permission mode, so a blocked command is still blocked, and Claude still sees the message and the reason. Auto mode does not turn the hook off.

### Optional: pr-review-toolkit

The `pr-review-toolkit` plugin is optional. When it is installed, the gate runs its `code-reviewer` agent for the code review step. Without it, the gate runs the same review in a general-purpose subagent. The run log names which reviewer ran, on the `GATE code-review` line.

### Update

```
/plugin update not-me@not-me
```

Do this when a new release is announced. If a release raises the config `schema` number, `/not-me:config` warns about older docs, and `/not-me:setup --upgrade` shows a diff before it changes them.

## The operator token

Some actions must be confirmed by a person, not by the program: allowing a repo (`allow-repo add` and `remove`), removing a pause (`pause off`) and going live (`mode live`). The operator token is how not-me tells the difference. It is a random secret kept in `~/.claude/not-me/operator-token`, readable only by you. Claude Code cannot run these actions and the guard blocks any attempt to read the file.

Create it once, in your own terminal app (for example the macOS Terminal app, not the terminal panel inside Claude Code):

```
<path to notme> doctor --init-token
```

`/not-me:mode` prints the exact path to use. The token is shown once. Save it in a password manager. When an action needs it, the command asks for the token and hides what you type. Scripts can set `NOT_ME_OPERATOR_CONFIRM` to the token instead.

If you lose it, delete `~/.claude/not-me/operator-token` and run `doctor --init-token` again. While the file exists, `--init-token` refuses and tells you to delete it first.

A new token also turns live mode off, because the live switch is signed with the old one (see [Going live](#going-live)). Run `/not-me:mode live` again after you make a new token.

## 4. First run

Run these in the repo you want to automate, in a Claude Code session started in that repo:

1. `/not-me:doctor` checks the tool versions, the effective config, and that the guard hook really blocks something. Fix anything it reports before you go on.
2. `/not-me:setup` asks a few questions and writes `.claude/not-me/*.md` in the repo. Commit those files. They are the only thing not-me asks you to commit. Setup also records the allowed repos on your allow list.
3. `/not-me --dry-run` runs the whole loop as if live. Reads are real. Every write goes into a ledger instead of GitHub.

A fresh install picks only issues assigned to you. Issues with no assignee are left alone until you set `queue: {include_unassigned: true}` in `~/.claude/not-me/project.md`. Issues assigned to someone else are never picked. See [config.md](config.md#which-issues-the-queue-picks).

Read the ledger before you trust it. It is `<git-common-dir>/not-me/logs/intended-writes.jsonl`, which is `.git/not-me/logs/intended-writes.jsonl` in a normal clone. Each line says which repo, which command, and what text it would have posted.

### Going live

Not-me starts in `mode: dry-run`. A repo file can set `dry-run` but can never set `live`, because a repo file is committed by anyone with push access. Only you can switch to live, from your own terminal.

Run `/not-me:mode live`. It checks whether you have an [operator token](#the-operator-token) and gives you one command to paste into your own terminal app. That command asks for the token and hides what you type. Then run `/not-me:mode` again to confirm it shows `live` with source `global`.

That command is the only way to go live. It sets `mode: live` in `~/.claude/not-me/project.md` and writes a small signature file next to it, `~/.claude/not-me/mode-live.sig`, made with your operator token and a random secret in `~/.claude/not-me/mode-live.epoch` (created the first time). Not-me runs live only when both are there and the signature matches your current token and epoch. Typing `mode: live` into the file by hand is not enough: not-me keeps running in dry-run and `/not-me:mode`, `/not-me:config` and `/not-me:doctor` warn that live is set but not confirmed. A new operator token makes the old signature invalid, so run `/not-me:mode live` again after you rotate it.

`/not-me:mode dry-run --global` switches back at any time: it deletes the signature and replaces the epoch with a new random one, so a copy of an old signature can never turn live back on. You can also set `mode: dry-run` by hand. Returning to dry-run by any means revokes the signature (the next time not-me reads its config), so going live again needs `/not-me:mode live`.

Upgrading from an older version: if your global `project.md` already says `mode: live`, not-me runs in dry-run until you run `/not-me:mode live` once.

## Uninstall cleanly

1. Remove the plugin and the marketplace:

   ```
   /plugin uninstall not-me@not-me
   /plugin marketplace remove not-me
   ```

2. Delete what not-me left on your machine. None of it is needed by anything else:

   | What | Where |
   | --- | --- |
   | Global config and allow list | `~/.claude/not-me/` (or the folder `NOT_ME_HOME` points to) |
   | Logs, claims, ledger, active-run marker | `<git-common-dir>/not-me/` in each repo, for example `.git/not-me/` |
   | Private temp files | the `not-me-<uid>` folder in your system temp directory |
   | Repo config | `.claude/not-me/` in each repo. It is committed, so remove it in a pull request |

Nothing else is written. Uninstalling does not touch your branches, pull requests, labels, or comments on GitHub.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `not-me: no working Python 3.9 or newer found (tried python3, python, py -3)` and exit code 7 | The launcher found no interpreter that runs and is 3.9 or newer. On Windows it is often only the Store stub | Install Python as in step 1. On Windows tick "Add python.exe to PATH". Restart Claude Code so it sees the new `PATH` |
| `not-me guard failed closed: <error>; run /not-me:doctor` | The hook crashed on a call that mentions git or GitHub, so it blocked the call rather than let it through unchecked | Run `/not-me:doctor`. Check `python3 --version` is 3.9 or newer and that the plugin folder is intact. `/plugin update not-me@not-me` reinstalls it |
| `FAILED - guard not working` at the start of a run | The self-test sent a command that must be blocked, and the hook did not block it | Run `/not-me:doctor`. Do not turn guard rules off to get past it. Fix the interpreter or reinstall the plugin |
| The hook blocked a command that is fine | A guard rule matched it. Often a write to another repo, or a script or `bash -c` that the hook cannot read | Read the message. It names the rule and what to do. For another repo, see "Allow a repo" in [guards.md](guards.md). Otherwise run the `git` or `gh` command directly instead of through a wrapper |
| Run stops with `STOP - paused` or `STOP - disabled` | A `PAUSED` file exists, or `enabled: false` is set in some layer | Remove the file, or fix the key. Only a person should do this |
| `config invalid` with a layer, file, and line | A config file has a typo or a bad value | Fix that line. `/not-me:config` shows what is in force |
| Nothing is picked up | The queue is empty, the label name does not match, or no ready issue is assigned to you | Check `labels.todo` matches a real label in your tracker. Assign the issue to yourself, or set `queue.include_unassigned: true` in your global layer. See [config.md](config.md#which-issues-the-queue-picks) |
| `gh` calls fail with a scope error | The token lacks `project` for a board queue | `gh auth refresh -s project` |
