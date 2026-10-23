"""PreToolUse guard: decide whether a tool call may run.

`decide(event, ctx)` returns None (allow) or a Block. It reads git state in the
working directory (push URLs, current branch, aliases) lazily through
`ctx.git`, and loads not-me config only when a rule needs it. Building the
context (`GuardContext.from_project`) and the inactive path start no
processes: the checkout is found by walking up for `.git`.

Outside a not-me project (`is_active` false) the four rule groups are off, but
self-protection still applies: the hook calls `ctx.self_only()`, which stubs
git and config so nothing forks, and `decide` keeps only self_protect blocks.

Rule groups, each switched by `guard.<rule>` in project.md:
  other_repo          writes may only target allowed repos
  protected_branches  no commits, pushes, merges or resets on protected branches
  force_push          no history rewrites or remote branch deletion
  destructive_git     shown as "destructive": hard resets, forced branch deletes,
                      clean -x, rm -rf .git, and GitHub settings, secrets and deletions
Self-protection (the allow list, the operator token, the live-mode marker,
allow-repo and pause off, the plugin's own files, not-me config during a run,
and `notme mode live`) is always on, in every directory, whether or not the
guard is active. Live mode is signed: `mode: live` counts only next to a marker
that `notme mode live` writes with the operator token, so no file edit can
turn it on. The guard still blocks Edit/Write of `mode: live` into the global
project.md as an early, friendly refusal; that check is not the security
boundary. Merge grants (notme/merge_grant.py) are self-protected the same way:
no tool may touch the merge-grants folder, and `decide` lifts the `gh pr merge`
block only for the exact PR a person granted from their own prompt, once.

Residual risk: a token file readable by the same OS user cannot be fully hidden
from an agent running as that user; this guard is the enforcement, and the
operator token is a second layer, not a secret.

Bash commands are split by `lex`, one linear pass that reads quotes,
backslashes, line continuations, substitutions, here-documents and
redirections the way the shell does; a command it cannot read with confidence,
longer than MAX_COMMAND or nested deeper than MAX_NESTING is blocked unread.
The guard does not follow cd: a command that changes directory anywhere (see
_dir_change) may not also write with git or gh, run notme, or use relative
paths a rule would have to resolve. Writes are judged in the hook's real
working directory, or the `git -C` / `gh -R` they name. Anything else it
cannot see through ($(...) around gh/git, GIT_DIR=..., a command name in a
variable, inline interpreter code, scripts that mention gh/git) is blocked, so
it fails closed. A string another program runs as a command (bash -c, eval,
awk system(), sed e, vim !, tar --to-command) is lexed and judged as a command
itself (_judge_body); one still obfuscated after lexing is blocked when it
mentions gh/git/github or the command could reach the network.
"""
from __future__ import annotations

import bisect
import fnmatch
import glob
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Mapping, Optional, Set, Tuple

from . import merge_grant, paths, runlog, writes
from .frontmatter import parse as parse_frontmatter  # stdlib only: no config stack
from .writes import parse_origin, valid_slug

RULES = writes.GUARD_RULES
RULE_LABEL = {"destructive_git": "destructive"}
SELF = "self_protect"
OPAQUE = "opaque"  # cannot be checked: reported under the first enabled rule

ALLOWLIST_NAME = writes.ALLOWLIST_FILE
BOARD_MARKER = "--not-me-board"
ACTIVE_RUN_STALE_HOURS = 6
CONFIG_HINT = ".claude/not-me/project.md"
PLUGIN_PROTECTED_DIRS = ("hooks", "bin", "notme", "cli", "skills")
SCRIPT_READ_LIMIT = 1 << 20
SCRIPT_SUFFIXES = (".sh", ".bash", ".zsh")
# The global layer's file that holds `mode`: the same name as layers.PROJECT_FILE, kept here so the hook does not
# import the config modules on every call.
GLOBAL_DOC = "project.md"
MAX_COMMAND = 32 * 1024  # longer Bash commands are blocked unread: checking them could outrun the hook timeout
MAX_NESTING = 64  # deeper $( ), backtick or ${ } nesting is blocked unread
MAX_BODY_DEPTH = 4  # shell bodies (bash -c, eval, awk system()) nested deeper than this are blocked
PREFILTER_MAX = 2048  # longer commands skip the regex prefilter and always get the full check

GitFn = Callable[[List[str], str], Optional[str]]

# --- patterns --------------------------------------------------------------

_HOST_SLUG = re.compile(r"^github\.com/([^/]+/[^/]+)$")
_GH_GIT = re.compile(r"(^|[^A-Za-z0-9_.-])(gh|git)([^A-Za-z0-9_.-]|$)")
_GITHUB = re.compile(r"github\.com", re.IGNORECASE)
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\[\]]*\])?\+?=")
_PATH_SEP = re.compile(r"[/\\]")
_SCP_REMOTE = re.compile(r"^[^/]*@[^/]*:")
_ALLOW_EDIT = re.compile(r"(?:^|[^A-Za-z0-9_-])(?:(add|remove)|(--e[a-z]*))(?:[^A-Za-z0-9_-]|$)")
_PARAM_DEFAULT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:?[-=+?]([^{}]*)\}")
_ESC_SEQ = re.compile(r"\\(?:x([0-9A-Fa-f]{1,2})|([0-7]{1,3})|u([0-9A-Fa-f]{4}))")  # \x67, \147, g
_SHELL_PIECES = re.compile(r"\$\{[^{}]*\}|\$\([^()]*\)|`[^`]*`|\$(?=['\"])")
_OBFUSCATED = re.compile(r"[$`\\'\"]")  # left in a word after lexing: the shell still expands or unquotes it
# a command that could reach the network: a URL, a network client, or GitHub
_NETWORK = re.compile(r"://|github\.com|(?<![A-Za-z0-9_.-])(?:curl|wget|nc|ncat|netcat|ssh|scp|sftp|rsync|telnet|"
                      r"socat|ftp|http|https|xh|gh|git)(?![A-Za-z0-9_-])", re.IGNORECASE)
_NOTME_PROGS = {"notme", "notme.py"}
# variables that re-point the not-me CLI's trust anchor (its project, home, operator or GitHub identity)
_TRUST_ENV = re.compile(r"(?<![A-Za-z0-9_])(NOT_ME_[A-Za-z0-9_]*|CLAUDE_PROJECT_DIR|CLAUDECODE|HOME|GH_HOST|GH_TOKEN|"
                        r"GITHUB_TOKEN)=")
_TRUST_UNSET = re.compile(r"(?<![A-Za-z0-9_-])(?:unset|-u|--unset)\s+(?:-[a-z]+\s+){0,8}(NOT_ME_[A-Za-z0-9_]*|"
                          r"CLAUDE_PROJECT_DIR|CLAUDECODE|HOME|GH_HOST|GH_TOKEN|GITHUB_TOKEN)\b")
OPERATOR_CONFIRM = "NOT_ME_OPERATOR_CONFIRM"
# VAR=value prefixes that may sit in front of gh, git or notme; any other one cannot be checked
_SAFE_ENV = {"LC_ALL", "LANG", "PYTHONUTF8", "PAGER", "GH_PAGER", "NO_COLOR", "TERM"}
_PAUSE_OK = ([], ["on"], ["status"])
OPERATOR_TOKEN = "operator-token"  # the human-confirm token file under the global not-me home
# the token-signed live-mode marker under the global not-me home (humans.LIVE_MARKER, kept literal so the hook
# imports no more than it needs); only `notme mode live`, run by a person, writes it
LIVE_MARKER_NAME = "mode-live.sig"
LIVE_MARKER_WHY = "the live-mode marker is the operator's own signature"
LIVE_HINT = "the operator runs /not-me:mode live in their own terminal"
# the random epoch that keys the marker together with the token (humans.LIVE_EPOCH); it is replaced on every return
# to dry-run, so the agent may neither read nor write it, exactly like the token
LIVE_EPOCH_NAME = "mode-live.epoch"
LIVE_EPOCH_WHY = "the live-mode epoch is the operator's own secret"
TOKEN_WHY, TOKEN_HINT = "the operator token is the operator's own confirmation", "ask the operator to confirm by hand"
GRANT_NAME = merge_grant.GRANT_DIR  # the merge-grant directory under the global not-me home
GRANT_WHY = "merge grants come only from the person typing " + merge_grant.COMMAND
GRANT_HINT = f"ask the operator; to allow one merge they send {merge_grant.COMMAND} owner/name#N"
# words that make a forged terminal dangerous: driving the not-me CLI or its human checks through a pty
_NOTME_HOME_PATH = r"\.claude[/\\]+not-me(?![A-Za-z0-9_.-])"  # the global not-me home (and the repo layer)
_PTY_TRIGGER = re.compile(r"notme|allow-repo|(?<![A-Za-z0-9_-])pause(?![A-Za-z0-9_-])|operator-token"
                          r"|mode-live\.(?:sig|epoch)|NOT_ME_|merge-grants|" + _NOTME_HOME_PATH)
# `mode` followed by anything but show or dry-run: the live switch, when the command word is a $VAR
_MODE_LIVE = re.compile(r"(?<![A-Za-z0-9_-])mode\s+(?!(?:show|dry-run)(?![A-Za-z0-9_-]))\S")
# words a script file run through a shell may not contain (they drive the not-me CLI where the guard cannot see)
_SCRIPT_SELF = re.compile(r"notme|allow-repo|operator-token|mode-live\.(?:sig|epoch)|NOT_ME_|merge-grants|"
                          + _NOTME_HOME_PATH)
_PTY_CODE = re.compile(r"(?<![A-Za-z])pty(?![A-Za-z])|openpty|forkpty|spawn\s*\(")
# code that forges a terminal or leaves the session's process tree: blocked in inline code and scripts outright
_PRIMITIVES = re.compile(r"(?<![A-Za-z_])pty(?![A-Za-z_])|openpty|forkpty|os\.fork\b|(?<![A-Za-z_.])fork\s*\(|setsid"
                         r"|(?<![A-Za-z_])daemon(?!\s*=)|start_new_session\s*=\s*True|detached\s*:\s*true")
GLOB_LIMIT = 5000  # paths a token-read glob may visit before the guard assumes it reaches the token
_MODE_OK = ("show", "dry-run")  # `notme mode` subcommands the agent may run
_PY_VALUE_OPTS = "WXQ"  # python short options that take a value (-W ignore, -Xutf8), skipped before -m or the script
_PTY_PROGS = {"script", "expect", "unbuffer", "socat", "ssh", "tmux", "screen"}
# running a command outside the session's process tree escapes the CLI's ancestry check
_ORPHAN_PREFIX = {"nohup", "setsid"}
_ORPHAN_PROGS = {"disown", "daemon", "start-stop-daemon", "systemd-run", "at", "batch", "cron"}
_BACKGROUND = re.compile(r"(?<![&<>|])&(?![&>])")  # a lone & (also zsh &! and ( ... ) &), not &&, 2>&1 or &>
# commands that read, copy or move files: none may reach the operator token through a glob or a directory
_TOKEN_READERS = {"cat", "head", "tail", "less", "more", "od", "xxd", "base64", "cp", "mv", "ln", "tac", "nl",
                  "strings", "hexdump", "grep", "egrep", "fgrep", "rg", "sed", "awk", "rsync", "tar", "zip", "scp",
                  "dd", "install", "diff", "cmp", "sort", "uniq", "cut", "paste", "tee", "file", "stat"}
_TOKEN_COPIERS = {"cp", "mv", "ln", "rsync", "tar", "zip", "scp", "install"}
_IN_REDIRECT = re.compile(r"(?<![<\d])\d*<(?![<(&])\s*([^\s;|&<>()]+)")
_OUT_REDIRECT = re.compile(r"(?:^|[^<>&\d])\d*&?>>?\|?(?![>&(])\s*([^\s;|&<>()]+)")
# shell text that leaves a path unresolved for the guard: $(...), backticks, ${...}, $VAR, {a,b} and a stray $
_UNRESOLVED = re.compile(r"\$\([^)]*\)?|`[^`]*`?|\$\{[^}]*\}?|\$[A-Za-z0-9_@*#?!-]+|\{[^}]*\}?|\$")
_HOME_PARAM = re.compile(r"\$\{(NOT_ME_HOME|HOME)(?![A-Za-z0-9_])[^}]*\}")
_GLOB = re.compile(r"[*?\[]")
_LOCALHOST = re.compile(r"^(?:[^@]+@)?(?:localhost|127(?:\.\d+){3}|::1|\[::1\]|0\.0\.0\.0)$", re.IGNORECASE)
_INTERP = re.compile(r"^(python[0-9.]*|pythonw|py|node|nodejs|perl|ruby|php|deno|bun)$")
_REPO_PATH = re.compile(r"^repos/([^/]+)/([^/?]+)(/|\?|$)")
_PULL_MERGE = re.compile(r"/pulls/\d+/merge/?$")
_PROTECTION = re.compile(r"/branches/[^/]+/protection(/|$)")
_SECRETS = re.compile(r"/(actions|dependabot|codespaces)/(secrets|variables)(/|$)")
_MCP_TOOL = re.compile(r"^mcp__.*github.*", re.IGNORECASE)
_READ_MCP_PREFIX = re.compile(r"^(get|list|search)_[a-z0-9_]+$")
_MCP_WRITE_WORDS = re.compile(
    r"(^|_)(merge|create|update|delete|add|remove|set|write|push|run|rerun|dismiss|request|submit|assign|close|"
    r"reopen|fork|edit|lock|unlock|cancel|enable|disable|trigger|mark|star|approve|comment|upload|transfer)(_|$)")
# Bash commands that cannot matter to any rule skip the full check (applied with quotes/backslashes removed).
_PREFILTER = re.compile(
    r"(?<![A-Za-z0-9_-])(gh|git|github|rm|find|xargs|parallel|curl|wget|python[0-9.]*|py|node|nodejs|perl|ruby|"
    r"php|deno|bun|bash|sh|zsh|dash|ksh|fish|source|exec|eval)(?![A-Za-z0-9_-])"
    r"|(?:^|[\s;|&(])\.(?:\s|$)|\./|\.(?:sh|bash|zsh)\b|\$|`|allow-repo|allowed-repos|GIT_|\.git|notme|not-me|NOT_ME_|operator-token"
    r"|mode-live"  # LIVE_MARKER_NAME
    r"|merge-grants"  # GRANT_NAME
    r"|(?<![A-Za-z0-9_-])(?:script|expect|unbuffer|socat|ssh|tmux|screen|pause)(?![A-Za-z0-9_-])"
    # any file reader, copier or < redirection, or a > redirection to a glob, brace or ~ path: its arguments may
    # expand into the not-me home (a plain > target that does not spell a guarded name cannot reach it)
    r"|>(?!&)\s*[^\s;|&<>()]*[*?\[{~]|(?<![<\d&])<(?![<(&])|(?<![A-Za-z0-9_-])(?:" + "|".join(sorted(_TOKEN_READERS)) + r")(?![A-Za-z0-9_-])"
    r"(?=[^;|&\n]*?\s(?![-<>\d])\S)")

_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
_FED_PROGS = _SHELLS | {"eval", "xargs", "sudo", "doas", "source", ".", "env", "parallel"}  # run their stdin
_KEYWORDS = {"builtin", "{", "}", "if", "then", "else", "elif", "do", "while", "until", "!", "coproc", "-"}
# prefix words that run the rest of the line, with their options that take a value
_PREFIX_WORDS: Dict[str, Set[str]] = {
    "env": {"-u", "--unset", "-C", "--chdir"},
    "time": set(),
    "nohup": set(),
    "command": set(),
    "exec": {"-a"},
    "nice": {"-n", "--adjustment"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "ionice": {"-c", "-n", "-p"},
    "stdbuf": {"-i", "-o", "-e"},
    "caffeinate": {"-t", "-w"},
    "setsid": set(),
    "unbuffer": set(),
    "chronic": set(),
    "noglob": set(),
    "nocorrect": set(),
}
# A command that changes the shell's directory anywhere may not also write with git or gh: the guard judges
# writes in the hook's real working directory and does not follow cd (see _dir_change).
_DIR_WORDS = {"cd", "pushd", "popd", "chdir"}  # as any word: also caught behind xargs, env, nohup, sudo, find
_DIR_NAMES = {"source", ".", "eval", "exec", "alias", "function", "trap", "emulate", "zle", "sched"}  # command name
_DIR_VARS = "PWD|OLDPWD|CDPATH|HOME|DIRSTACK|cdpath|dirstack"
# zsh special arrays that define functions, aliases or options (auto_cd) when assigned to
_ZSH_TABLES = "functions|aliases|galiases|saliases|options"
_DIR_VAR_SET = re.compile(r"(?<![A-Za-z0-9_$])(?:" + _DIR_VARS + "|" + _ZSH_TABLES
                          + r")(?:\[[^\[\]]*\])?\s*(?:[-+*/%&|^]|<<|>>)?=(?!=)")
_DIR_VAR_WORD = re.compile(r"(?:-[A-Za-z]*)?(?:" + _DIR_VARS + r")")
_GLOB_QUALIFIER = re.compile(r"\((?:#q)?[^()\s|;&]*?(?:e[^A-Za-z0-9_\s]|\+[A-Za-z_])")  # zsh *(e:'cd x':), *(+f)
_DIR_VAR_DEFAULT = re.compile(r"\$\{(?:" + _DIR_VARS + r")(?:\[[^\[\]]*\])?:?=")
_DECLARE = {"declare", "typeset", "local", "export", "readonly", "integer", "float", "private"}
_NAME_ARGS = {"read", "mapfile", "readarray", "unset", "let", "getopts"}
_CD_HINT = ("use git -C <path> or gh -R owner/name instead of cd; if a cd is needed, run it in a separate Bash "
            "call first")
_HTTP_CLIENTS = {"curl", "wget", "http", "https", "xh"}
# programs that never run their arguments: a gh or git word there is data (anything else naming gh/git blocks)
# (not man, more, sort or yq: a pager or --compress-program can run a command; rg, awk, sed, vim, tar and less
# have their own handlers, which look at the options and program text that run one)
_INERT = {"echo", "printf", "grep", "egrep", "fgrep", "cat", "ls", "head", "tail", "wc", "test", "[", "[[",
          "true", "false", ":", "which", "type", "whereis", "file", "stat", "uniq",
          "cut", "tr", "diff", "cmp", "basename", "dirname", "realpath", "readlink", "touch", "mkdir", "cp", "mv",
          "ln", "chmod", "jq", "tee", "comm", "column", "nl", "od", "xxd", "hexdump", "strings", "base64",
          "shasum", "sha256sum", "md5", "md5sum", "date", "pwd", "whoami", "id", "uname", "sleep", "rmdir",
          "cd", "pushd", "popd", "export", "unset", "local", "read", "set", "rg"}
# git subcommands that change nothing, provided every option is on _GIT_READ_OPTS. A git call is a read
# only when proven to be one; an unknown subcommand, option or -c key blocks.
_GIT_READ_ONLY = {"", "status", "log", "diff", "show", "rev-parse", "ls-files", "ls-tree", "ls-remote", "cat-file",
                  "grep", "blame", "annotate", "describe", "shortlog", "rev-list", "merge-base", "show-ref",
                  "for-each-ref", "show-branch", "check-ignore", "name-rev", "cherry", "range-diff", "diff-tree",
                  "diff-files", "diff-index", "whatchanged", "count-objects", "var", "help", "version"}
_GIT_READ_OPTS = set("""
-- -p -u -s -z -v -q -n -i -w -b -l -L -M -C -B -D -R -a -r -t -d -c -e -E -F -G -S -h -H -I -o -W -U -A -m -1
--oneline --stat --shortstat --numstat --name-only --name-status --patch --no-patch --raw --graph --decorate
--no-decorate --all --branches --tags --remotes --abbrev-commit --no-abbrev-commit --pretty --reverse --first-parent
--merges --no-merges --follow --cached --staged --color --no-color --check --exit-code --quiet --summary --full-index
--minimal --patience --histogram --pickaxe-all --pickaxe-regex --no-renames --no-ext-diff --no-textconv --left-right
--cherry-pick --cherry-mark --boundary --ancestry-path --simplify-by-decoration --topo-order --date-order
--author-date-order --full-history --source --mailmap --no-mailmap --use-mailmap --show-signature --no-walk
--walk-reflogs --log-size --full-diff --short --branch --porcelain --long --ignored --show-stash --ahead-behind
--no-ahead-behind --column --no-column --renames --find-renames --find-copies --irreversible-delete
--ignore-all-space --ignore-space-change --ignore-blank-lines --ignore-cr-at-eol --ignore-space-at-eol
--invert-grep --all-match --regexp-ignore-case --basic-regexp --extended-regexp --fixed-strings --perl-regexp
--line-number --ignore-case --files-with-matches --files-without-match --count --word-regexp --invert-match
--untracked --no-index --heading --break --only-matching --full-name --null --show-toplevel --git-dir
--git-common-dir --absolute-git-dir --abbrev-ref --verify --symbolic-full-name --symbolic --is-inside-work-tree
--is-inside-git-dir --is-bare-repository --show-prefix --show-cdup --show-superproject-working-tree --sq --others
--exclude-standard --modified --deleted --stage --directory --unmerged --error-unmatch --killed --full-tree --heads
--refs --get-url --symref --is-ancestor --octopus --independent --fork-point --hash --dereference --exists --head
--contains --merged --no-merged --points-at --non-matching --verbose --always --dirty --broken --exact-match
--numbered --email --committer --line-porcelain --incremental --root --show-email --show-number --show-name --list
--no-notes --left-only --right-only --not --and --or --unified --word-diff --color-words --relative --no-relative
--function-context --text --expand-tabs --no-expand-tabs --batch-check --no-prefix --max-count
""".split())
# --name=value forms accepted on reads
_GIT_READ_VALUE_OPTS = set("""
--format --pretty --date --since --until --after --before --author --committer --grep --max-count --skip --stat
--dirstat --abbrev --diff-filter --unified --word-diff --word-diff-regex --color --color-words --relative --decorate
--find-renames --find-copies --untracked-files --ignore-submodules --porcelain --sort --max-depth --context
--after-context --before-context --exclude --encoding --min-parents --max-parents --branches --tags --remotes
--glob --candidates --match --default --short --points-at --contains --merged --no-merged --group --type
--inter-hunk-context --src-prefix --dst-prefix --line-prefix --submodule --column --count
""".split())
_GIT_SAFE_SHORT = frozenset("pusvqnizwblLMCBDRartdcehHEFIoWUAm1234567890")  # letters in -sb style clusters
# git -c keys (and git config writes) that cannot run code or redirect a repo; every other key blocks
_GIT_SAFE_KEYS = {"user.name", "user.email", "core.quotepath", "core.abbrev", "init.defaultbranch", "safe.directory",
                  "commit.gpgsign", "tag.gpgsign", "log.showsignature", "log.decorate", "diff.renames",
                  "diff.noprefix", "pull.rebase", "pull.ff", "merge.ff", "rebase.autosquash", "rebase.autostash",
                  "fetch.prune", "status.short", "status.branch"}
_GIT_SAFE_KEY_PREFIXES = ("color.", "advice.", "column.", "i18n.")
# git options before the subcommand that are understood; any other one blocks
_GIT_GLOBALS = {"--no-pager", "-p", "-P", "--paginate", "--no-optional-locks", "--literal-pathspecs",
                "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs", "--no-replace-objects",
                "--no-lazy-fetch", "--version", "--help", "--html-path", "--man-path", "--info-path"}
# local writes allowed outside a directory change; their code-running options block (see _git_exec_option)
_GIT_LOCAL = {"fetch", "pull", "add", "restore", "stash", "tag", "worktree", "init", "clone", "cherry-pick",
              "revert", "mv", "rm", "apply", "format-patch", "reflog"}
_GIT_EXEC_OPTS: Dict[str, Tuple[str, ...]] = {
    "clone": ("--upload-pack", "-u", "--template", "-c", "--config"), "init": ("--template",),
    "fetch": ("--upload-pack",), "pull": ("--upload-pack",), "rebase": ("-x", "--exec"), "ls-remote": ("-u",),
}
_MERGE_STRATEGIES = {"ort", "recursive", "resolve", "octopus", "ours", "subtree"}
# write subcommands with an option that runs a program: only these exact long options pass (git also accepts any
# unambiguous prefix, so --ex would be --exec); --upload-pack, --template, --config and --exec are left out
_MERGE_LONG = set("""
--commit --no-commit --edit --no-edit --cleanup --ff --no-ff --ff-only --gpg-sign --no-gpg-sign --log --no-log
--signoff --no-signoff --stat --no-stat --squash --no-squash --verify --no-verify --strategy --strategy-option
--verify-signatures --no-verify-signatures --summary --no-summary --quiet --verbose --progress --no-progress
--autostash --no-autostash --allow-unrelated-histories --rerere-autoupdate --no-rerere-autoupdate
""".split())
_FETCH_LONG = set("""
--all --no-all --append --atomic --depth --deepen --shallow-since --shallow-exclude --unshallow --update-shallow
--negotiation-tip --negotiate-only --dry-run --porcelain --write-fetch-head --no-write-fetch-head --force --keep
--multiple --auto-maintenance --no-auto-maintenance --auto-gc --no-auto-gc --write-commit-graph
--no-write-commit-graph --prefetch --prune --no-prune --prune-tags --no-tags --tags --refetch --refmap
--no-recurse-submodules --jobs --set-upstream --update-head-ok --progress --no-progress --server-option
--show-forced-updates --no-show-forced-updates --ipv4 --ipv6 --quiet --verbose --filter
""".split())
_GIT_WRITE_LONG: Dict[str, Set[str]] = {
    "fetch": _FETCH_LONG,
    "pull": _FETCH_LONG | _MERGE_LONG | {"--rebase", "--no-rebase"},
    "merge": _MERGE_LONG | {"--abort", "--quit", "--continue", "--message", "--file", "--into-name",
                            "--overwrite-ignore", "--no-overwrite-ignore"},
    "rebase": set("""
--onto --keep-base --no-keep-base --continue --skip --abort --quit --edit-todo --show-current-patch --apply --merge
--empty --keep-empty --no-keep-empty --reapply-cherry-picks --no-reapply-cherry-picks --allow-empty-message
--strategy --strategy-option --gpg-sign --no-gpg-sign --quiet --verbose --stat --no-stat --verify --no-verify
--force-rebase --no-ff --fork-point --no-fork-point --ignore-whitespace --whitespace --committer-date-is-author-date
--ignore-date --reset-author-date --signoff --interactive --rebase-merges --no-rebase-merges --update-refs
--no-update-refs --autosquash --no-autosquash --autostash --no-autostash --root --rerere-autoupdate
--no-rerere-autoupdate --reschedule-failed-exec --no-reschedule-failed-exec
""".split()),
    "cherry-pick": set("""
--edit --no-edit --cleanup --mainline --no-commit --signoff --gpg-sign --no-gpg-sign --ff --allow-empty
--allow-empty-message --keep-redundant-commits --empty --strategy --strategy-option --rerere-autoupdate
--no-rerere-autoupdate --continue --skip --quit --abort
""".split()),
    "revert": set("""
--edit --no-edit --cleanup --mainline --no-commit --signoff --gpg-sign --no-gpg-sign --strategy --strategy-option
--rerere-autoupdate --no-rerere-autoupdate --reference --continue --skip --quit --abort
""".split()),
    "clone": set("""
--local --no-local --no-hardlinks --shared --reference --reference-if-able --dissociate --quiet --verbose
--progress --server-option --no-checkout --bare --sparse --filter --also-filter-submodules --mirror --origin
--branch --revision --depth --shallow-since --shallow-exclude --single-branch --no-single-branch --no-tags --tags
--recurse-submodules --shallow-submodules --no-shallow-submodules --remote-submodules --no-remote-submodules
--separate-git-dir --jobs --bundle-uri --ref-format
""".split()),
    "init": {"--quiet", "--bare", "--object-format", "--ref-format", "--separate-git-dir", "--initial-branch",
             "--shared"},
}
# subcommands that only list with these arguments: (words, True = every argument, False = the first one)
_GIT_LISTING: Dict[str, Tuple[Set[str], bool]] = {
    "branch": ({"-a", "-r", "-v", "-vv", "-l", "--list", "--all", "--remotes", "--verbose", "--show-current"}, True),
    "tag": ({"-l", "--list", "-n"}, True),
    "stash": ({"list", "show"}, False),
    "worktree": ({"list"}, False),
    "reflog": ({"", "show", "list"}, False),
}
# variables that can make git or gh run a program or read other config: setting them by any means next to a
# git or gh call blocks, except the values below
_ENV_NAMES = (r"GIT_[A-Z0-9_]*|EDITOR|VISUAL|PAGER|MANPAGER|GH_[A-Z0-9_]*|XDG_CONFIG_HOME|HOME|LD_PRELOAD"
              r"|LD_LIBRARY_PATH|DYLD_[A-Z0-9_]*|PATH|BASH_ENV|ENV|PROMPT_COMMAND|IFS|LESSOPEN|LESSCLOSE|BROWSER"
              r"|SSH_ASKPASS")
_ENV_ASSIGN = re.compile(r"(" + _ENV_NAMES + r")(?:\[[^\[\]]*\])?\+?=(.*)", re.S)
_ENV_WORD = re.compile(r"(?:-[A-Za-z]*)?(?:" + _ENV_NAMES + r")")
# ${NAME:=value} or ${NAME=value} assigns NAME from inside any word; ${!ref:=value} assigns a computed name
_ENV_DEFAULT = re.compile(r"\$\{(?:(?:" + _ENV_NAMES + r")(?:\[[^\[\]]*\])?|![^{}:=]*):?=")
# an assignment inside (( )) or $(( ))
_ENV_ARITH = re.compile(r"\(\([^()]*?(?<![A-Za-z0-9_$])(?:" + _ENV_NAMES + r")(?:\[[^\[\]]*\])?\s*"
                        r"(?:[-+*/%&|^]|<<|>>)?=(?!=)")
_ENV_SETTERS = {"export", "declare", "typeset", "local", "readonly", "integer", "float", "private", "unset", "read",
                "mapfile", "readarray", "getopts", "let", "for", "select", "printf", "env"}
_SAFE_EXEC_ENV = {"PAGER": {"", "cat", "less", "more"}, "GH_PAGER": {"", "cat", "less", "more"},
                  "GIT_PAGER": {"", "cat", "less", "more"}}
_SAFE_GIT_ENV = {"GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS"}
_GH_READ_VERBS = {"view", "list", "diff", "checks", "search", "browse", "clone", "download", "watch", "checkout",
                  "status", ""}
_GH_READ_GROUPS = {"config", "help", "completion", "search", "version", "status"}
_GH_GROUPS = {"pr", "issue", "repo", "release", "label", "run", "workflow", "secret", "variable", "cache",
              "project", "ruleset", "browse", "codespace", "org", "attestation", "gpg-key", "ssh-key"}
# group -> write verbs that are always blocked (None = every verb): settings, secrets and deletion are human actions
_GH_DESTRUCTIVE: Dict[str, Optional[Set[str]]] = {
    "secret": {"set", "delete", "remove"},
    "variable": {"set", "delete", "remove"},
    "issue": {"delete", "transfer"},
    "repo": {"edit", "delete", "rename", "archive", "unarchive"},
    "release": {"delete", "delete-asset"},
    "label": {"delete"},
    "workflow": {"disable"},
    "ruleset": None,
}
# gh api flags that take a value -> what the value is (None = ignored)
_API_VALUE_FLAGS: Dict[str, Optional[str]] = {
    "-X": "method", "--method": "method", "-f": "field", "-F": "field", "--field": "field",
    "--raw-field": "field", "--input": "input", "-H": None, "--header": None, "-q": None, "--jq": None,
    "-t": None, "--template": None, "--hostname": None, "--cache": None, "-p": None, "--preview": None,
}
_XARGS_VALUE = {"-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a", "--max-args", "--max-procs", "--delimiter",
                "--arg-file", "--replace", "--max-lines", "--eof", "--max-chars"}
_PARALLEL_VALUE = {"-j", "-S", "-P", "-I", "-a", "--jobs", "--sshlogin", "--arg-file"}
_SUDO_VALUE = {"-u", "-g", "-C", "-D", "-h", "-p", "-U", "-r", "-t", "--user", "--group", "--chdir"}
_FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}
_READ_MCP = {
    "get_me", "get_file_contents", "get_commit", "list_commits", "list_branches", "list_tags", "get_tag",
    "list_releases", "get_latest_release", "get_release_by_tag", "get_issue", "get_issue_comments", "issue_read",
    "list_issues", "search_issues", "list_issue_types", "list_sub_issues", "get_label", "list_label",
    "get_pull_request", "get_pull_request_files", "get_pull_request_diff", "get_pull_request_status",
    "get_pull_request_comments", "get_pull_request_reviews", "get_pull_request_review_comments",
    "pull_request_read", "list_pull_requests", "search_pull_requests", "search_code", "search_repositories",
    "search_users", "search_orgs", "list_workflows", "list_workflow_runs", "get_workflow_run",
    "get_workflow_run_logs", "list_workflow_jobs", "get_job_logs", "list_workflow_run_artifacts",
    "get_code_scanning_alert", "list_code_scanning_alerts", "get_secret_scanning_alert",
    "list_secret_scanning_alerts", "get_dependabot_alert", "list_dependabot_alerts", "list_discussions",
    "get_discussion", "get_discussion_comments", "list_discussion_categories", "list_notifications",
    "get_notification_details",
}

# --- results ---------------------------------------------------------------


@dataclass
class Block:
    rule: str
    reason: str
    target: Optional[str] = None
    command: str = ""
    hint: Optional[str] = None  # what to do instead
    suggest_allow: bool = False  # target is a real owner/name the operator could allow
    tool: str = ""
    message: str = ""
    grantable: bool = False  # the plain `gh pr merge` block a person's merge grant may lift


Decision = Optional[Block]  # None = allow

# --- helpers ---------------------------------------------------------------


def repo_slug(value: Any) -> Optional[str]:
    """owner/name from a slug, a github URL (as gh reads it) or github.com/owner/name; None otherwise."""
    if not isinstance(value, str) or not value:
        return None
    if valid_slug(value):
        return value
    m = _HOST_SLUG.match(value)
    return m.group(1) if m else parse_origin(value, allow_path=True)


def has_gh_git(text: str) -> bool:
    return bool(_GH_GIT.search(text))


def _mentions_github(text: str) -> bool:
    return has_gh_git(text) or bool(_GITHUB.search(text))


def _base(word: str) -> str:
    return _PATH_SEP.split(word)[-1]


def _head(word: str) -> str:
    """Command name: basename, with gh/git recognised through backslashes, case and .exe."""
    for cand in (_base(word), _base(word.replace("\\", ""))):
        low = cand.lower()
        low = low[:-4] if low.endswith(".exe") else low
        if low in ("gh", "git"):
            return low
    return _base(word)


def _unshell(text: str) -> str:
    """Text as the shell would mostly see it: quotes and backslashes gone, ${X:-word} replaced by word."""
    return _PARAM_DEFAULT.sub(r"\1", text.replace('"', "").replace("'", "").replace("\\", ""))


def _short(text: str, limit: int = 100) -> str:
    one = " ".join(text.split()).replace("`", "'")
    return one if len(one) <= limit else one[: limit - 3] + "..."


_path = os.path  # every guard path comparison goes through this module (tests swap in ntpath)


def _resolve(p: str) -> str:
    """realpath of p; a part that does not exist yet is joined onto its deepest existing ancestor's realpath,
    so short names (RUNNER~1) and symlinks above it resolve the same way as for an existing path."""
    tail: List[str] = []
    here = _path.normpath(p)
    while not _path.exists(here):
        parent, name = _path.split(here)
        if not name or parent == here:
            break
        tail.append(name)
        here = parent
    try:
        here = _path.realpath(here)
    except (OSError, ValueError):
        pass
    return _path.join(here, *reversed(tail)) if tail else here


def _norm(p: str, base: str) -> str:
    """The single path normaliser: unquote, expand ~, make absolute against base, resolve, normcase."""
    p = _path.expanduser(p.replace('"', "").replace("'", ""))
    if not _path.isabs(p):
        p = _path.join(base, p)
    return _path.normcase(_path.normpath(_resolve(p)))


def _under(path: str, base: str) -> bool:
    """path is base or inside it; both must come from _norm."""
    sep = _path.sep
    return path == base or path.startswith(base.rstrip(sep) + sep)


def _short_flags(args: List[str]) -> str:
    return "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--") and len(a) > 1)


def _positionals(args: List[str]) -> List[str]:
    out, after = [], False
    for a in args:
        if after or not a.startswith("-"):
            out.append(a)
        elif a == "--":
            after = True
    return out


def _skip_options(args: List[str], takes_value: Set[str]) -> List[str]:
    """args after the leading options (and their values) of a wrapper like xargs or sudo."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in takes_value else 1
    return args[i:]


class Unparsed(ValueError):
    """The command cannot be split into words with confidence: the caller blocks it."""


# git subcommands a line may hold for its commit and tag messages to count as data
_DATA_GIT_SUBS = {"add", "commit", "tag", "status", "diff"}
_MSG_OPT = re.compile(r"^(?:-[A-Za-z]*m|--message)$")  # -m, -am, --message: the message is the next word
_STDIN_FILE_OPTS = (["-F", "-"], ["--file", "-"])


def _data_only(cmd: str) -> str:
    """cmd without the git commit/tag messages in it: -m values and a here-document fed to git commit -F -.

    Used for the self-protection word scans, so a message that names not-me commands is not mistaken for
    running them. Only a line of plain git add/commit/tag/status/diff commands (only -C before the
    subcommand) with no pipe, substitution or backslash qualifies, and a message with $ or a backtick is
    kept; anything else returns cmd unchanged. The lexer splits the words, so a comment or a quote cannot
    hide a second command.
    """
    if "\\" in cmd or "|" in cmd.replace("||", "") or any(x in cmd for x in ("$(", "`", "<(", ">(")):
        return cmd
    try:
        parsed = lex(cmd)
    except Unparsed:
        return cmd
    if parsed.subs or parsed.funcdef:
        return cmd
    out: List[str] = []
    for c in parsed.commands:
        w = [str(x) for x in c.words]
        if not w or c.nested or _prog(w[0]) != "git":
            return cmd
        k = 1
        while w[k:k + 1] == ["-C"] and len(w) > k + 1:
            k += 2
        sub = w[k] if k < len(w) else ""
        if sub and sub not in _DATA_GIT_SUBS:
            return cmd
        message = sub in ("commit", "tag")
        stdin_msg = message and ("--file=-" in w or any(w[j:j + 2] in _STDIN_FILE_OPTS for j in range(k, len(w))))
        if any(op not in ("<<", "<<-") or not stdin_msg for op, _ in c.redirs):
            return cmd  # the only redirection allowed is a here-document fed to git commit -F -
        kept: List[str] = []
        value = False
        for x in w:
            raw = "$" in x or "`" in x  # kept: it may expand
            if value:
                kept.append(x if raw else "''")
                value = False
            elif message and x.startswith("--message="):
                kept.append(x if raw else "--message=''")
            else:
                kept.append(x)
                value = message and bool(_MSG_OPT.match(x))
        out.append(" ".join(kept))
        if not stdin_msg:
            out.extend(c.bodies)
    return "\n".join(out)


class _Word(str):
    """A word with its quoting removed. `glob` and `brace` mark an unquoted glob or {a,b} brace expansion."""
    glob = False
    brace = False
    quoted = False


@dataclass
class SimpleCommand:
    """One simple command: its words (unquoted, redirections removed), its source text and its input data."""
    words: List[str]
    raw: str = ""
    span: Tuple[int, int] = (0, 0)
    bodies: List[str] = field(default_factory=list)  # here-document bodies and here-strings fed to it
    redirs: List[Tuple[str, str]] = field(default_factory=list)  # (operator, target)
    nested: bool = False  # inside $( ), backticks or <( )


@dataclass
class Parsed:
    commands: List[SimpleCommand]  # every simple command, nested ones included
    subs: List[str]  # source of every $( ), backtick, <( ) and >( ) body, here-document bodies removed
    code: str  # the whole command without here-document bodies
    funcdef: bool  # a name() function definition


_META = frozenset(" \t\n;&|()<>")
_REDIR_OP = re.compile(r"&>>?|<<<|<<-?|<>|<&|>&|>>|>\||<|>")
_FD_DUP = re.compile(r"\d*-?(?=[\s;&|()<>]|$)")
_FD_WORD = re.compile(r"\d+|\{[A-Za-z_][A-Za-z0-9_]*\}")
_FUNCDEF = re.compile(r"\(\s*\)")
_ANSI_ESC = re.compile(r"\\(?:([abeEfnrtv\\'\"?])|([0-7]{1,3})|x([0-9A-Fa-f]{1,2})|u([0-9A-Fa-f]{1,4})"
                       r"|U([0-9A-Fa-f]{1,8})|c(.)|(.))", re.S)
_ANSI = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}


def _ansi(m: "re.Match[str]") -> str:
    simple, octal, hexa, u4, u8, ctl, other = m.groups()
    if simple is not None:
        return _ANSI.get(simple, simple)
    if ctl is not None:
        return chr(ord(ctl) & 0x1F)
    if other is not None:
        return "\\" + other
    code = int(octal, 8) if octal is not None else int(hexa or u4 or u8, 16)
    if code > 0x10FFFF:
        raise Unparsed("a $'...' escape is out of range")
    return chr(code)


class _Lexer:
    """One left-to-right pass over a Bash command, splitting it the way the shell does: quotes,
    backslashes, line continuations, $( ), backticks, ${ }, $(( )), here-documents and comments.
    Linear in the length of the command; nesting deeper than MAX_NESTING raises Unparsed."""

    def __init__(self, text: str):
        self.s, self.i, self.n = text, 0, len(text)
        self.commands: List[SimpleCommand] = []
        self.sub_spans: List[Tuple[int, int, bool]] = []  # (start, end, is ${ })
        self.cuts: List[Tuple[int, int]] = []  # here-document bodies, as (start, end) spans
        self.pending: List[Tuple[str, bool, bool, SimpleCommand]] = []  # (delimiter, strip tabs, quoted, cmd)
        self.funcdef = False
        self.depth = 0

    def nest(self, delta: int) -> None:
        self.depth += delta
        if self.depth > MAX_NESTING:
            raise Unparsed(f"it is nested more than {MAX_NESTING} deep")

    def parse(self, stop: Optional[str]) -> None:
        """Commands up to the end of the text, or up to and including `stop` (")" or "`")."""
        s, n = self.s, self.n
        cmd = SimpleCommand([], nested=self.depth > 0)
        start, parens = self.i, 0

        def flush() -> SimpleCommand:
            if s[start:self.i].strip():
                cmd.span = (start, self.i)
                self.commands.append(cmd)
            return SimpleCommand([], nested=self.depth > 0)

        while True:
            if self.i >= n:
                if stop:
                    raise Unparsed(f"no closing {stop}")
                flush()
                return
            c = s[self.i]
            if c == "\\" and s.startswith("\n", self.i + 1):  # a line continuation
                self.i += 2
            elif c in " \t":
                self.i += 1
            elif c == "#":  # a comment (words are read whole, so this is a word start)
                end = s.find("\n", self.i)
                self.i = n if end < 0 else end
            elif c == "`" and stop == "`":
                flush()
                self.i += 1
                return
            elif c == ")":
                cmd = flush()
                self.i += 1
                if parens:
                    parens -= 1
                elif stop == ")":
                    return
                start = self.i
            elif c == "(":
                if cmd.words and not cmd.words[-1].endswith("=") and _FUNCDEF.match(s, self.i):
                    self.funcdef = True
                cmd = flush()
                self.i += 1
                parens += 1
                start = self.i
            elif c == "&" and s.startswith(">", self.i + 1):
                self.redirect(cmd, stop)
            elif c in ";&|\n":
                cmd = flush()
                self.i += 1
                if c == "\n":
                    self.read_bodies()
                else:
                    while self.i < n and s[self.i] in ";&|":
                        self.i += 1
                start = self.i
            elif c in "<>" and not s.startswith("(", self.i + 1):
                self.redirect(cmd, stop)
            else:
                w = self.word(stop)
                if not (self.i < n and s[self.i] in "<>" and not w.quoted and _FD_WORD.fullmatch(w)):
                    cmd.words.append(w)  # else an fd number, such as the 2 in 2>&1

    def redirect(self, cmd: SimpleCommand, stop: Optional[str]) -> None:
        s = self.s
        m = _REDIR_OP.match(s, self.i)
        op = m.group(0) if m else s[self.i]
        self.i += len(op)
        if op in ("<&", ">&"):
            dup = _FD_DUP.match(s, self.i)
            if dup and dup.end() > self.i:  # 2>&1, >&-
                self.i = dup.end()
                return
        while self.i < self.n and (s[self.i] in " \t" or s.startswith("\\\n", self.i)):
            self.i += 2 if s[self.i] == "\\" else 1
        if self.i >= self.n or (s[self.i] in _META and not s.startswith("(", self.i + 1)):
            raise Unparsed(f"{op} has no target")
        begin = self.i
        target = self.word(stop)
        if op in ("<<", "<<-"):
            quoted = any(q in s[begin:self.i] for q in "'\"\\")
            self.pending.append((str(target), op == "<<-", quoted, cmd))
        elif op == "<<<":
            cmd.bodies.append(str(target))
        cmd.redirs.append((op, str(target)))

    def read_bodies(self) -> None:
        """Here-document bodies start after the line that opened them."""
        s = self.s
        while self.pending:
            delim, strip, quoted, cmd = self.pending.pop(0)
            start = pos = self.i
            body_end = resume = self.n
            while pos < self.n:
                eol = s.find("\n", pos)
                eol = self.n if eol < 0 else eol
                line = s[pos:eol]
                if (line.lstrip("\t") if strip else line) == delim:
                    body_end, resume = pos, min(eol + 1, self.n)
                    break
                pos = eol + 1
            cmd.bodies.append(s[start:body_end])
            self.cuts.append((start, resume))
            if not quoted:  # an unquoted delimiter: $( ), backticks and ${ } in the body run
                self.expand(body_end)
            self.i = resume

    def expand(self, end: int) -> None:
        s = self.s
        while self.i < end:
            c = s[self.i]
            if c == "\\":
                self.i += 2
            elif c == "`":
                self.sub(1, "`")
            elif c == "$" and s.startswith("(", self.i + 1):
                self.dollar_paren()
            elif c == "$" and s.startswith("{", self.i + 1):
                self.param(dq=True)
            else:
                self.i += 1
        if self.i > end:
            raise Unparsed("an expansion runs past the end of its here-document")

    def word(self, stop: Optional[str]) -> _Word:
        s, n = self.s, self.n
        out: List[str] = []
        glob = quoted = bracket = False
        brace = 0  # 1 after an unquoted {, 2 after a , or .. inside it, 3 after the closing }
        while self.i < n:
            c = s[self.i]
            if c in _META:
                if c in "<>" and s.startswith("(", self.i + 1):  # <( ) and >( ) process substitution
                    out.append(self.sub(2, ")"))
                    continue
                break
            if c == "`":
                if stop == "`":
                    break
                out.append(self.sub(1, "`"))
            elif c == "\\":
                nxt = s[self.i + 1:self.i + 2]
                if nxt == "`" and stop == "`":
                    raise Unparsed("it nests backticks")
                self.i += 2
                if nxt != "\n":
                    out.append(nxt)
                    quoted = True
            elif c == "'":
                end = s.find("'", self.i + 1)
                if end < 0:
                    raise Unparsed("a single quote is not closed")
                out.append(s[self.i + 1:end])
                self.i = end + 1
                quoted = True
            elif c == '"':
                out.append(self.dquote(stop))
                quoted = True
            elif c == "$" and s.startswith("'", self.i + 1):
                out.append(self.ansi())
                quoted = True
            elif c == "$" and s.startswith('"', self.i + 1):
                self.i += 1
            elif c == "$" and s.startswith("(", self.i + 1):
                out.append(self.dollar_paren())
            elif c == "$" and s.startswith("{", self.i + 1):
                out.append(self.param())
            else:
                if c in "*?" or (c == "]" and bracket):
                    glob = True
                bracket = bracket or c == "["
                if c == "{" and brace == 0:
                    brace = 1
                elif brace == 1 and (c == "," or s.startswith("..", self.i)):
                    brace = 2
                elif c == "}" and brace == 2:
                    brace = 3
                out.append(c)
                self.i += 1
        w = _Word("".join(out))
        w.glob, w.brace, w.quoted = glob, brace == 3, quoted
        return w

    def dquote(self, stop: Optional[str]) -> str:
        s = self.s
        out: List[str] = []
        self.i += 1
        while True:
            if self.i >= self.n:
                raise Unparsed("a double quote is not closed")
            c = s[self.i]
            if c == '"':
                self.i += 1
                return "".join(out)
            if c == "\\":
                nxt = s[self.i + 1:self.i + 2]
                if nxt == "`" and stop == "`":
                    raise Unparsed("it nests backticks")
                if nxt == "\n":
                    self.i += 2
                elif nxt and nxt in '$`"\\':
                    out.append(nxt)
                    self.i += 2
                else:
                    out.append(c)
                    self.i += 1
            elif c == "`":
                out.append(self.sub(1, "`"))
            elif c == "$" and s.startswith("(", self.i + 1):
                out.append(self.dollar_paren())
            elif c == "$" and s.startswith("{", self.i + 1):
                out.append(self.param(dq=True))
            else:
                out.append(c)
                self.i += 1

    def ansi(self) -> str:
        s, j = self.s, self.i + 2
        while j < self.n and s[j] != "'":
            j += 2 if s[j] == "\\" else 1
        if j >= self.n:
            raise Unparsed("a $'...' quote is not closed")
        text = _ANSI_ESC.sub(_ansi, s[self.i + 2:j])
        self.i = j + 1
        return text

    def sub(self, skip: int, close: str) -> str:
        """$( ), <( ), >( ) or a backtick substitution starting at self.i; returns its source text."""
        start = self.i
        self.nest(1)
        self.i += skip
        body = self.i
        self.parse(close)
        self.nest(-1)
        self.sub_spans.append((body, self.i - 1, False))
        return self.without_bodies(start, self.i)

    def dollar_paren(self) -> str:
        if self.s.startswith("((", self.i + 1):
            return self.arith()
        return self.sub(2, ")")

    def arith(self) -> str:
        """$(( ... )); a $( (...) ... ) that only starts like one is a command substitution."""
        s, start = self.s, self.i
        j, depth = self.i + 3, 0
        while j < self.n:
            c = s[j]
            if c in "'\"`\\" or (c == "$" and s.startswith("(", j + 1)):
                raise Unparsed("it has quotes or substitutions inside $(( ))")
            if c == "(":
                depth += 1
            elif c == ")":
                if depth:
                    depth -= 1
                elif s.startswith(")", j + 1):
                    self.i = j + 2
                    return s[start:self.i]
                else:
                    return self.sub(2, ")")
            j += 1
        raise Unparsed("no closing )) for $((")

    def param(self, dq: bool = False) -> str:
        s, start = self.s, self.i
        self.nest(1)
        self.i += 2
        while True:
            if self.i >= self.n:
                raise Unparsed("no closing } for ${")
            c = s[self.i]
            if c == "}":
                self.i += 1
                break
            if c == "\\":
                self.i += 2
            elif c == "'" and not dq:  # inside double quotes a ' is a plain character
                end = s.find("'", self.i + 1)
                if end < 0:
                    raise Unparsed("a single quote is not closed")
                self.i = end + 1
            elif c == '"':
                self.dquote(None)
            elif c == "`":
                self.sub(1, "`")
            elif c == "$" and s.startswith("(", self.i + 1):
                self.dollar_paren()
            elif c == "$" and s.startswith("{", self.i + 1):
                self.param(dq)
            else:
                self.i += 1
        self.nest(-1)
        self.sub_spans.append((start + 2, self.i - 1, True))  # its text is checked like a substitution's
        return s[start:self.i]

    def without_bodies(self, a: int, b: int) -> str:
        """The source between a and b with the here-document bodies inside it removed."""
        k = bisect.bisect_left(self.cuts, (a, -1))
        out, last = [], a
        while k < len(self.cuts) and self.cuts[k][1] <= b:
            out.append(self.s[last:self.cuts[k][0]])
            last = self.cuts[k][1]
            k += 1
        out.append(self.s[last:b])
        return "".join(out)


def lex(cmd: str) -> Parsed:
    """Split a Bash command the way the shell does. Raises Unparsed when that cannot be done with confidence."""
    if len(cmd) > MAX_COMMAND:
        raise Unparsed(f"it is longer than {MAX_COMMAND} characters")
    lx = _Lexer(cmd)
    lx.parse(None)
    lx.read_bodies()  # a here-document opened on the last line has an empty body
    for c in lx.commands:
        c.raw = lx.without_bodies(*c.span)
    subs = [lx.without_bodies(a, b) for a, b, _ in lx.sub_spans]
    subs = [_PARAM_OP.sub(" ", s, count=1) if p else s for s, (_, _, p) in zip(subs, lx.sub_spans)]
    return Parsed(lx.commands, subs, lx.without_bodies(0, len(cmd)), lx.funcdef)


# the name and operator at the start of a ${...} body, so the word after ${x:-word} reads as a word
_PARAM_OP = re.compile(r"^[#!]?[A-Za-z0-9_@*?$-]*?(?:\[[^\[\]]*\])?(?::?[-=+?]|##?|%%?|//?|\^\^?|,,?)")


def needs_check(event: Mapping[str, Any]) -> bool:
    """False for Bash commands that no rule can care about (cheap, no processes)."""
    if event.get("tool_name") != "Bash":
        return True
    tin = event.get("tool_input")
    if not isinstance(tin, dict):
        return True
    cmd = str(tin.get("command") or "")
    if len(cmd) > PREFILTER_MAX:
        return True
    try:
        parsed = lex(cmd)
    except Unparsed:
        return True
    if parsed.subs:  # a here-document body can hold a substitution the code text no longer shows
        return True
    text = parsed.code + "\n" + parsed.code.replace("\\\n", "")
    return bool(_PREFILTER.search(text.replace("'", "").replace('"', "").replace("\\", "")))


# --- finding the checkout without git ------------------------------------------


class _Ambiguous(Exception):
    pass


def find_checkout(start: str) -> Optional[Tuple[str, Path, Path]]:
    """(toplevel, git dir, common dir) by walking up for .git; None outside git.

    Raises _Ambiguous when a .git file cannot be read as `gitdir: <path>`.
    """
    here = Path(os.path.realpath(os.path.abspath(start)))
    for d in (here, *here.parents):
        g = d / ".git"
        if g.is_dir():
            return str(d), g, g
        if g.is_file():
            try:
                text = g.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as exc:
                raise _Ambiguous(str(exc)) from exc
            if not text.startswith("gitdir:"):
                raise _Ambiguous(f"{g} is not a gitdir file")
            gd = Path(text[len("gitdir:"):].strip())
            gd = gd if gd.is_absolute() else d / gd
            common = gd
            try:
                c = Path((gd / "commondir").read_text(encoding="utf-8").strip())
                common = c if c.is_absolute() else gd / c
            except OSError:
                pass
            return str(d), Path(os.path.realpath(gd)), Path(os.path.realpath(common))
    return None


def head_branch(git_dir: Path) -> Optional[str]:
    """Current branch from <git dir>/HEAD; None when detached or unreadable."""
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else None


def log_root(common_dir: Optional[Path]) -> Optional[Path]:
    """The default run-log root (<git common dir>/not-me/logs), found without git; None outside git."""
    return Path(common_dir) / "not-me" / "logs" if common_dir else None


# --- context ---------------------------------------------------------------

class GuardContext:
    """Where the call runs. The write gate (`writes.GuardContext`: origin, allowed repos, rules,
    protected branches) is built from config on first use; pass `write_ctx` to fix it."""

    def __init__(self, *, cwd: str = ".", root: Optional[str] = None, git_dir: Optional[Path] = None,
                 common_dir: Optional[Path] = None, config_dir: Optional[Path] = None,
                 allowlist_path: Optional[Path] = None, active_run: bool = False,
                 plugin_root: Optional[Path] = None, home: Optional[Path] = None,
                 env: Optional[Mapping[str, str]] = None, git: GitFn = paths.run_git,
                 write_ctx: Optional[writes.GuardContext] = None):
        self.env = os.environ if env is None else env
        self.cwd = str(cwd)
        self.root = root
        self.git_dir = git_dir
        self.common_dir = common_dir
        self.config_dir = Path(config_dir) if config_dir else Path(root or self.cwd) / ".claude" / "not-me"
        self.home = Path(home) if home else paths.home_dir(self.env)
        self.allowlist_path = Path(allowlist_path) if allowlist_path else writes.allowlist_path(self.home)
        self.active_run = active_run
        self.plugin_root = Path(plugin_root) if plugin_root else paths.plugin_root(self.env)
        self.git = git
        self.only_self = False
        if write_ctx is not None:
            self.__dict__["write_ctx"] = write_ctx  # shadows the cached property

    def self_only(self) -> "GuardContext":
        """Outside a not-me project: keep only self-protection; git and config are stubbed so nothing forks."""
        self.only_self = True
        self.git = _no_git
        self.__dict__["write_ctx"] = writes.GuardContext()  # no config: the rules it feeds are dropped anyway
        return self

    @classmethod
    def from_project(cls, project_dir: Any, home: Optional[Any] = None, env: Optional[Mapping[str, str]] = None,
                     git: GitFn = paths.run_git) -> "GuardContext":
        """Build the context for a checkout without starting a process (git only if the walk is ambiguous)."""
        env = os.environ if env is None else env
        cwd = str(project_dir)
        ambiguous = False
        try:
            found = find_checkout(cwd)
        except _Ambiguous:
            found, ambiguous = None, True
        if found is None and (ambiguous or env.get("GIT_DIR")):
            out = git(["rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir"], cwd)
            parts = out.splitlines() if out else []
            if len(parts) == 3:
                top, git_dir, common = (paths._abs_git_path(cwd, p) for p in parts)
                found = (str(top), git_dir, common)
        root, git_dir, common = found if found else (None, None, None)
        config_dir = Path(root or cwd) / ".claude" / "not-me"
        if not config_dir.is_dir() and git_dir is not None and git_dir != common and common.name == ".git":
            main = common.parent / ".claude" / "not-me"
            if main.is_dir():
                config_dir = main
        active = common is not None and runlog.active_run(runlog.marker_path_in(common),
                                                          stale_hours=ACTIVE_RUN_STALE_HOURS) is not None
        return cls(cwd=cwd, root=root, git_dir=git_dir, common_dir=common, config_dir=config_dir,
                   home=Path(home) if home else None, env=env, active_run=active, git=git)

    @cached_property
    def write_ctx(self) -> writes.GuardContext:
        from . import config  # deferred: most calls never need config

        base = self.root or self.cwd
        cfg = config.load(project_dir=str(base), home=str(self.home), env=self.env)
        origin_url = self.git(["remote", "get-url", "origin"], base) or ""  # "" stops writes asking git again
        return writes.GuardContext.from_config(cfg, origin_url=origin_url, checkout=base)

    @property
    def origin_repo(self) -> Optional[str]:
        origin = self.write_ctx.origin_repo
        return origin.lower() if origin else None

    @property
    def allowed_repos(self) -> FrozenSet[str]:
        return self.write_ctx.allowed_repos_lower

    @property
    def rules(self) -> Mapping[str, bool]:
        return self.write_ctx.rules

    @cached_property
    def protected_branches(self) -> FrozenSet[str]:
        return frozenset(self.write_ctx.protected_branches)


def _no_git(args: List[str], cwd: str) -> Optional[str]:
    return None


def is_active(ctx: GuardContext) -> bool:
    """The guard applies in a not-me project, or anywhere a not-me run is active."""
    return ctx.active_run or Path(ctx.config_dir).is_dir()


# --- message ---------------------------------------------------------------


def render(block: Block, ctx: GuardContext) -> str:
    label = RULE_LABEL.get(block.rule, block.rule)
    head = f"not-me guard [{label}]: blocked `{block.command}`"
    lines = [head + (f": targeting {block.target}." if block.target else ".")]
    if block.suggest_allow and block.target:
        t = block.target
        allowed = ", ".join(sorted(ctx.allowed_repos)) or "nothing (no origin found)"
        lines += [f"This repo ({ctx.origin_repo or 'unknown origin'}) may write only to: {allowed}.",
                  f"If {t} is intended, the operator runs:  /not-me:allow-repo {t}",
                  f"  (all projects: /not-me:allow-repo {t} --everywhere; team-wide: list it under repos "
                  f"in {CONFIG_HINT}, then each operator confirms it once with /not-me:allow-repo {t})"]
    else:
        lines.append(f"Why: {block.reason}.")
        if block.hint:
            lines.append(f"Instead: {block.hint}.")
    lines.append("Autonomous runs: do not retry; log a QUESTION naming the operator and continue.")
    if block.rule in RULES:
        lines.append(f"Relax this rule (not recommended): set guard.{block.rule}: off and "
                     f"confirm_guard_off: [{block.rule}] in {CONFIG_HINT}")
    lines.append("See effective config: /not-me:config")
    return "\n".join(lines)


# --- decision --------------------------------------------------------------

Blk = Callable[..., Block]


def _blk(shown: str) -> Blk:
    def blk(rule: str, reason: str, hint: Optional[str] = None, target: Optional[str] = None,
            suggest: bool = False) -> Block:
        return Block(rule, reason, target=target, command=shown, hint=hint, suggest_allow=suggest)
    return blk


def decide(event: Mapping[str, Any], ctx: GuardContext) -> Decision:
    tool = event.get("tool_name") or ""
    tin = event.get("tool_input") or {}
    if not isinstance(tool, str) or not isinstance(tin, dict):
        raise TypeError("tool_name must be a string and tool_input an object")
    cwd = str(event.get("cwd") or ctx.cwd)
    if tool == "Bash":
        cands = _bash(str(tin.get("command") or ""), cwd, ctx)
    elif tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        cands = _file(tool, str(tin.get("file_path") or tin.get("notebook_path") or ""), cwd, ctx, tin)
    elif tool in _READ_TOOLS:
        cands = _read_tool(tool, tin, cwd, ctx)
    elif _MCP_TOOL.match(tool):
        cands = _mcp(tool, tin, ctx)
    else:
        return None
    # outside a project an unreadable command still blocks when it could drive the not-me CLI or its trust inputs
    plain = _unshell(str(tin.get("command") or "")) if ctx.only_self and tool == "Bash" else ""
    drives = bool(plain) and bool(_PTY_TRIGGER.search(plain) or _MODE_LIVE.search(plain))
    grant = _merge_grant(event, ctx) if tool == "Bash" else None
    lifted = False
    for block in cands:
        if grant is not None and block.grantable:
            lifted = True  # the person granted exactly this merge; every other check still applies
            continue
        if ctx.only_self and block.rule == OPAQUE and drives:
            block.rule = SELF
        if ctx.only_self and block.rule != SELF:
            continue
        if block.rule == OPAQUE:
            enabled = [r for r in RULES if ctx.rules.get(r, True)]
            if not enabled:
                continue
            block.rule = enabled[0]
        elif block.rule != SELF and not ctx.rules.get(block.rule, True):
            continue
        block.tool = tool
        block.message = render(block, ctx)
        return block
    if lifted and grant is not None:
        return _use_grant(ctx, grant, str(tin.get("command") or ""))
    return None


# --- merge grants -------------------------------------------------------------
#
# A person grants one named merge with /not-me:allow-merge owner/name#N (see notme/merge_grant.py). The guard lifts
# its `gh pr merge` block for exactly that PR, once, in that session, outside a not-me run, when the command is the
# plainest possible form and the repo is allowed.

_MERGE_UNQUOTED = re.compile(r"[A-Za-z0-9 \t_./=:@+,%-]*")  # what may stand outside quotes in a granted merge
_MERGE_METHODS = {"--squash", "--merge", "--rebase"}
_MERGE_VALUE_OPTS = {"--subject", "--body-file"}


def _merge_command(cmd: str, cwd: str, ctx: GuardContext) -> Optional[Tuple[str, int, Optional[str]]]:
    """(owner/name, number, method flag or None) when cmd is exactly one simple `gh pr merge <N> -R|--repo
    <owner/name>` with at most one of --squash, --merge or --rebase and optionally --subject and --body-file once
    each; else None. No substitution, variable, backslash, redirection, pipe, list, comment, glob or other option
    anywhere."""
    if any(c in cmd for c in "$`\\\n\r"):
        return None
    outside, quote = [], ""
    for c in cmd:
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        else:
            outside.append(c)
    if quote or not _MERGE_UNQUOTED.fullmatch("".join(outside)):
        return None
    try:
        parsed = lex(cmd)
    except Unparsed:
        return None
    if len(parsed.commands) != 1 or parsed.subs or parsed.funcdef:
        return None
    c = parsed.commands[0]
    if c.nested or c.bodies or c.redirs:
        return None
    w = [str(x) for x in c.words]
    if w[:3] != ["gh", "pr", "merge"]:
        return None
    number: Optional[int] = None
    repo: Optional[str] = None
    seen: Set[str] = set()
    i = 3
    while i < len(w):
        a = w[i]
        if a in ("-R", "--repo") or a in _MERGE_VALUE_OPTS:
            key = "--repo" if a == "-R" else a
            if key in seen or i + 1 >= len(w) or w[i + 1].startswith("-") or not w[i + 1]:
                return None
            seen.add(key)
            value = w[i + 1]
            if key == "--repo":
                if not valid_slug(value):
                    return None
                repo = value
            elif key == "--body-file" and _under(_norm(value, cwd), _norm(str(ctx.home), cwd)):
                return None  # never post a file from the not-me home
            i += 2
            continue
        if a in _MERGE_METHODS and not (seen & _MERGE_METHODS):
            seen.add(a)
        elif number is None and a.isdigit() and a.isascii() and not a.startswith("0"):
            number = int(a)
        else:
            return None
        i += 1
    if number is None or repo is None:
        return None
    method = next(iter(seen & _MERGE_METHODS), None)
    return repo, number, method


def _merge_grant(event: Mapping[str, Any], ctx: GuardContext) -> Optional[Tuple[str, str, int]]:
    """(session key, owner/name, number) when a fresh, signed grant for this call's session names exactly the PR
    this command merges and the method it uses (the grant's method, else --squash), the command is the plain
    form, the repo is allowed, and no not-me run is active; else None. Outside a not-me project the merge rule is
    off anyway, so a grant is neither needed nor used there."""
    tin = event.get("tool_input") or {}
    cmd = str(tin.get("command") or "")
    if ctx.only_self or ctx.active_run or "merge" not in cmd:
        return None
    cwd = str(event.get("cwd") or ctx.cwd)
    target = _merge_command(cmd, cwd, ctx)
    sid = merge_grant.session_key(event.get("session_id"))
    if target is None or sid is None:
        return None
    grant = merge_grant.read(ctx.home, sid)
    if grant is None:
        return None
    repo, number, method = target
    if grant["repo"].lower() != repo.lower() or grant["number"] != number or repo.lower() not in ctx.allowed_repos:
        return None
    if method != merge_grant.required_flag(grant):
        return None
    return sid, repo.lower(), number


def _use_grant(ctx: GuardContext, grant: Tuple[str, str, int], cmd: str) -> Decision:
    """Spend the grant (single use) and log it; block when another call spent it first."""
    sid, repo, number = grant
    if not merge_grant.consume(ctx.home, sid):
        block = _blk(_short(cmd))("protected_branches", "this merge grant was already used",
                                   f"ask the operator to send {merge_grant.COMMAND} {repo}#{number} again")
        block.tool, block.message = "Bash", render(block, ctx)
        return block
    root = log_root(ctx.common_dir)
    if root is not None:
        runlog.guard_log_append(runlog.guard_log_path(root), {
            "tool": "Bash", "rule": "merge_grant", "reason": "a person's merge grant allowed this merge",
            "target": f"{repo}#{number}", "session": sid})
    return None


def _check_target(ctx: GuardContext, target: str, blk: Blk) -> Iterator[Block]:
    """Block unless target (a named repo) is allowed."""
    if target.lower() in ctx.allowed_repos:
        return
    if not valid_slug(target):
        yield blk("other_repo", f"cannot read repo '{target}'", "spell out the repo as owner/name")
    else:
        yield blk("other_repo", f"{target} is not an allowed repo", target=target, suggest=True)


def _unknown_target(blk: Blk, why: str, fix: str) -> Block:
    return blk("other_repo", f"could not tell which repo it targets ({why})", fix)


_READ_TOOLS = ("Read", "NotebookRead", "LS", "Grep", "Glob")


# The not-me home's private files, as (name, why, hint). The agent may neither read nor write the secrets (the
# operator token and the live-mode epoch); the shell checks also cover the live marker, which the agent may not
# write (the Read tool may still read it).
_HOME_SECRETS = ((OPERATOR_TOKEN, TOKEN_WHY, TOKEN_HINT), (LIVE_EPOCH_NAME, LIVE_EPOCH_WHY, LIVE_HINT),
                 (GRANT_NAME, GRANT_WHY, GRANT_HINT))  # a directory: everything under it is guarded too
_HOME_GUARDED = _HOME_SECRETS + ((LIVE_MARKER_NAME, LIVE_MARKER_WHY, LIVE_HINT),)
# (_norm path, plain normcased path, why, hint) of one guarded file
_Guarded = Tuple[str, str, str, str]


def _guarded(ctx: GuardContext, base: str, entries: Tuple[Tuple[str, str, str], ...]) -> List[_Guarded]:
    out = []
    for name, why, hint in entries:
        plain = _path.join(str(ctx.home), name)
        out.append((_norm(plain, base), _path.normcase(_path.normpath(_path.expanduser(plain))), why, hint))
    return out


def _pattern_hit(pattern: str, base: str, guarded: List[_Guarded], recursive: bool) -> Optional[_Guarded]:
    """The guarded file a shell pattern (relative to base) could name, or None. fnmatch's * also crosses "/",
    so this over-approximates: a pattern that could reach the file counts."""
    p = _path.expanduser(pattern)
    if not _path.isabs(p) and not p.startswith("*"):
        p = _path.join(base, p)
    p = _path.normcase(_path.normpath(p))
    pats = [p] + ([p.rstrip(_path.sep) + _path.sep + "*"] if recursive else [])
    for g in guarded:
        # a file inside the grant directory, which may not exist yet
        inside = [_path.join(c, "s.json") for c in g[:2]] if _base(g[0]) == GRANT_NAME else []
        if any(fnmatch.fnmatchcase(c, q) for c in list(g[:2]) + inside for q in pats):
            return g
    return None


def _read_tool(tool: str, tin: Mapping[str, Any], cwd: str, ctx: GuardContext) -> Iterator[Block]:
    """Read, NotebookRead, LS, Grep and Glob may not reach the operator token or the live-mode epoch (or list the
    not-me home)."""
    if tool in ("Read", "NotebookRead"):
        yield from _token_file(tool, str(tin.get("file_path") or tin.get("notebook_path") or ""), cwd, ctx)
        return
    raw = str(tin.get("path") or "")
    here = _norm(cwd, os.getcwd())
    base = _norm(_expand_home(raw, ctx), here) if raw else here
    home = _norm(str(ctx.home), cwd)
    pattern = str(tin.get("pattern" if tool == "Glob" else "glob") or "")
    blk = _blk(_short(f"{tool} {raw} {pattern}".strip()))
    for g, (name, why, hint) in zip(_guarded(ctx, cwd, _HOME_SECRETS), _HOME_SECRETS):
        secret = g[0]
        hit = base in (home, secret) or _under(base, secret) or name in raw.lower() or name in pattern.lower()
        if not hit and tool != "LS" and _under(secret, base):
            rel = secret[len(base):].lstrip(_path.sep).replace(_path.sep, "/")
            # a missing or matching filter reaches the secret (fnmatch's * also crosses "/": an over-approximation)
            hit = not pattern or any(fnmatch.fnmatch(x, pattern) for x in (rel, name))
        if not hit and tool == "Glob" and pattern:
            full = _norm(_expand_home(pattern, ctx), base)
            hit = (full in (home, secret) or _under(full, home)
                   or (bool(_GLOB.search(pattern)) and _pattern_hit(full, base, [g], False) is not None))
        if hit:
            yield blk(SELF, why, hint)
            return


def _token_file(tool: str, fpath: str, cwd: str, ctx: GuardContext) -> Iterator[Block]:
    """The operator-token and live-mode epoch files are the operator's own: the agent may not read or change them."""
    if not fpath:
        return
    full = _norm(fpath, cwd)
    for g, (name, why, hint) in zip(_guarded(ctx, cwd, _HOME_SECRETS), _HOME_SECRETS):
        if _base(fpath).lower() == name or _under(full, g[0]):
            yield _blk(_short(f"{tool} {fpath}"))(SELF, why, hint)
            return
    if _reaches_grant(fpath, full, cwd, ctx):
        yield _blk(_short(f"{tool} {fpath}"))(SELF, GRANT_WHY, GRANT_HINT)


def _reaches_grant(fpath: str, full: str, cwd: str, ctx: GuardContext) -> bool:
    """A file tool's path names or reaches the merge-grant directory: by name anywhere in the path, under it
    after resolving symlinks physically (so `link/..` cannot step around it), or as a hard link to a grant."""
    if GRANT_NAME in fpath.lower() or GRANT_NAME in full.lower():
        return True
    gdir = _norm(_path.join(str(ctx.home), GRANT_NAME), cwd)
    p = _path.expanduser(fpath)
    try:
        physical = _path.normcase(_path.realpath(p if _path.isabs(p) else _path.join(cwd, p)))
    except (OSError, ValueError):
        return True
    if _under(physical, gdir):
        return True
    key = _stat_key(physical)
    if key is None or not _path.isdir(gdir):
        return False
    try:
        names = os.listdir(gdir)
    except OSError:
        return True
    return any(_stat_key(_path.join(gdir, n)) == key for n in names)


@dataclass(frozen=True)
class _Target:
    """The global project.md, worked out once per decision: its _norm path, that path lowercased, and its
    (device, inode) when it exists."""

    path: str
    low: str
    key: Optional[Tuple[int, int]]


def _stat_key(path: str) -> Optional[Tuple[int, int]]:
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return st.st_dev, st.st_ino


def _global_target(ctx: GuardContext, base: str) -> _Target:
    path = _norm(_path.join(str(ctx.home), GLOBAL_DOC), base)
    return _Target(path, path.lower(), _stat_key(path))


def _is_global_project(full: str, target: _Target) -> bool:
    """full (from _norm) is the global layer's project.md: compared without case (a case-insensitive file system
    opens it under any case) or, when both exist, as the same file (a hard link)."""
    if full.lower() == target.low:
        return True
    return target.key is not None and _stat_key(full) == target.key


_ABSENT = object()  # no top-level mode key
_UNKNOWN = object()  # the text could not be read or parsed


def _mode_of(text: Optional[str]) -> Any:
    """The top-level `mode` of a project.md text exactly as the config loader reads it (the real frontmatter
    parser, so quoted keys count): its value, _ABSENT, or _UNKNOWN when the text is missing or does not parse."""
    if text is None:
        return _UNKNOWN
    try:
        meta, _ = parse_frontmatter(text, source=GLOBAL_DOC)
        return meta.get("mode", _ABSENT)
    except Exception:  # FrontmatterError, RecursionError on deep nesting, anything else: unknown, so blocked
        return _UNKNOWN


def _read_text(path: str) -> Optional[str]:
    """A file's text; "" when it does not exist; None when it cannot be read or is too large to check."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(SCRIPT_READ_LIMIT + 1)
    except FileNotFoundError:
        return ""
    except (OSError, ValueError):
        return None
    return None if len(data) > SCRIPT_READ_LIMIT else data.decode("utf-8", "replace")


def _after_edit(tool: str, tin: Mapping[str, Any], current: Optional[str]) -> Optional[str]:
    """The file text a Write, Edit or MultiEdit would leave; None when that cannot be worked out (any other tool,
    an unreadable file, an old_string that is not there exactly), which the caller treats as unsafe."""
    if tool == "Write":
        content = tin.get("content")
        return content if isinstance(content, str) else None
    if tool not in ("Edit", "MultiEdit") or current is None:
        return None
    edits = tin.get("edits") if tool == "MultiEdit" else [tin]
    if not isinstance(edits, list):
        return None
    text = current
    for e in edits:
        old = e.get("old_string") if isinstance(e, dict) else None
        new = e.get("new_string") if isinstance(e, dict) else None
        if not isinstance(old, str) or not isinstance(new, str) or old not in text:
            return None
        text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
    return text


def _global_mode(tool: str, tin: Mapping[str, Any], full: str, blk: Blk) -> Iterator[Block]:
    """An early, friendly refusal (not the security boundary: live mode also needs the token-signed marker, which
    no file edit can make). An agent Write/Edit/MultiEdit of the global project.md is allowed only when, parsed as
    the config loader parses it, the result says `mode: dry-run`, or has no mode and had none before. Anything else
    (live, null, any other value, a result that does not parse, an edit that cannot be worked out) is blocked."""
    current = _read_text(full)
    try:
        after = _mode_of(_after_edit(tool, tin, current))
    except Exception:
        after = _UNKNOWN
    if after == "dry-run" or (after is _ABSENT and _mode_of(current) is _ABSENT):
        return
    yield blk(SELF, "only a person turns live mode on" + ("" if after is not _UNKNOWN else
                                                          " (this edit to the global project.md cannot be checked)"),
              LIVE_HINT)


def _names_live_marker(fpath: str, full: str, ctx: GuardContext, cwd: str) -> bool:
    return _base(fpath).lower() == LIVE_MARKER_NAME or full.lower() == _norm(
        _path.join(str(ctx.home), LIVE_MARKER_NAME), cwd).lower()


def _file(tool: str, fpath: str, cwd: str, ctx: GuardContext, tin: Mapping[str, Any]) -> Iterator[Block]:
    if not fpath:
        return
    yield from _token_file(tool, fpath, cwd, ctx)
    blk = _blk(_short(f"{tool} {fpath}"))
    full = _norm(fpath, cwd)
    if _names_live_marker(fpath, full, ctx, cwd):
        yield blk(SELF, LIVE_MARKER_WHY, LIVE_HINT)
    if _is_global_project(full, _global_target(ctx, cwd)):
        yield from _global_mode(tool, tin, full, blk)
    if full == _norm(str(ctx.allowlist_path), cwd) or _base(fpath) == ALLOWLIST_NAME:
        yield blk(SELF, "only the operator changes the not-me allow list", "ask the operator to run /not-me:allow-repo")
    plugin = _norm(str(ctx.plugin_root), cwd)
    if any(_under(full, _norm(d, plugin)) for d in PLUGIN_PROTECTED_DIRS):
        yield blk(SELF, "the not-me plugin's own code is off limits to the agent",
                  "describe the change in a QUESTION for the operator")
    if ctx.active_run:
        home = _norm(str(ctx.home), cwd)
        if _under(full, _norm(str(ctx.config_dir), cwd)) or (_path.dirname(full) == home and full.endswith(".md")):
            yield blk(SELF, "not-me config is read-only while a not-me run is active",
                      "log a QUESTION proposing the config change for the operator")
    low = fpath.replace("\\", "/").lower()
    parts = low.split("/")
    if ".git" in parts or parts[-1] == ".gitconfig" or "/.config/git/" in low or low.startswith(".config/git/"):
        yield blk("other_repo", "git config and hooks can change where git pushes",
                  "change git settings by hand, outside the agent")


def _mcp(tool: str, tin: Mapping[str, Any], ctx: GuardContext) -> Iterator[Block]:
    name = tool.split("__")[-1]
    if name in _READ_MCP or (_READ_MCP_PREFIX.match(name) and not _MCP_WRITE_WORDS.search(name)):
        return
    blk = _blk(_short(tool))
    owner, repo = tin.get("owner"), tin.get("repo")
    if isinstance(owner, str) and owner and isinstance(repo, str) and repo:
        target: Optional[str] = f"{owner}/{repo}"
    else:
        target = repo_slug(repo)
    if target:
        yield from _check_target(ctx, target, blk)
    else:
        yield _unknown_target(blk, "not a known read-only tool and names no owner/repo",
                              "pass owner and repo naming an allowed repo")


class _State:
    """Per-call state: the hook's real working directory, whether the command changes it, the branch a
    checkout moved to, and memoised lookups. Text scans see both the source and its line continuations joined.

    A shell body judged as a command (bash -c, eval, awk system()) gets a child state: its text, commands,
    directory change and background are its own plus every parent's, since what the parent set reaches it."""

    def __init__(self, cwd: str, parsed: Parsed, raw: str, parent: Optional["_State"] = None):
        self.dir = cwd
        self.parsed = parsed
        self.depth = parent.depth + 1 if parent else 0
        text = parsed.code + "\n" + parsed.code.replace("\\\n", "")
        own_raw = raw + "\n" + raw.replace("\\\n", "")  # with here-document bodies, for the pty checks
        self.text = parent.text + "\n" + text if parent else text
        self.raw = parent.raw + "\n" + own_raw if parent else own_raw
        self.commands: List[SimpleCommand] = (parent.commands if parent else []) + parsed.commands
        self.background = bool(_BACKGROUND.search(text)) or bool(parent and parent.background)
        self.moved = _moves(parsed, raw, cwd) or (parent.moved if parent else None)  # what changes the directory
        self.switched: Dict[str, str] = parent.switched if parent else {}  # a checkout outlives the child shell
        self.memo: Dict[Tuple[str, ...], Any] = {}

    def once(self, key: Tuple[str, ...], fn: Callable[[], Any]) -> Any:
        if key not in self.memo:
            self.memo[key] = fn()
        return self.memo[key]

    def find(self, pattern: "re.Pattern[str]", text: str) -> "Optional[re.Match[str]]":
        """pattern.search(text), once per call: whole-command scans must not repeat per simple command."""
        return self.once(("find", pattern.pattern, str(id(text))), lambda: pattern.search(text))

    @cached_property
    def plain_text(self) -> str:
        return _unshell(self.text)

    @cached_property
    def plain_raw(self) -> str:
        return _unshell(self.raw)


def _moves(parsed: Parsed, raw: str, cwd: str) -> Optional[str]:
    """The first thing in the command that may change the shell's directory, or None."""
    if parsed.funcdef:
        return "a function definition"
    m = _DIR_VAR_DEFAULT.search(raw) or _GLOB_QUALIFIER.search(parsed.code)
    if m:
        return m.group(0)
    for c in parsed.commands:
        found = _dir_change(c.words, cwd)
        if found:
            return _short(found, 60)
    return None


def _dir_change(words: List[str], cwd: str) -> Optional[str]:
    """What in one simple command may change the shell's directory, or None. When in doubt, it counts."""
    for w in words:
        if w in _DIR_WORDS or _DIR_VAR_SET.search(w) or _DIR_VAR_WORD.fullmatch(w):
            return w
    rest = list(words)
    while rest:  # the command name, past assignments, keywords and prefix words
        w, p = rest[0], _prog(rest[0])
        if p in _DIR_NAMES or p in _DIR_WORDS:
            return w
        if p in ("env", "sudo", "doas"):
            flag = "C" if p == "env" else "D"
            for a in rest[1:]:
                if a.startswith("--chdir") or (a.startswith("-") and not a.startswith("--") and flag in a):
                    return f"{p} {a}"
                if not a.startswith("-") and not _ASSIGN.match(a):
                    break
        if _ASSIGN.match(w) or p in _KEYWORDS:
            rest = rest[1:]
        elif p in _PREFIX_WORDS:
            rest = _drop_prefix(p, rest[1:])
        else:
            break
    if not rest:
        return None
    name, p, args = rest[0], _prog(rest[0]), rest[1:]
    if p == "find" and {"-execdir", "-okdir"} & set(args):
        return "find -execdir"
    if p in _DECLARE and any((a.startswith("-") and "n" in a[1:] and not a.startswith("--"))
                             or "$" in a.split("=", 1)[0] or "`" in a.split("=", 1)[0] for a in args):
        return f"{name} with a reference or a computed name"
    if p in _NAME_ARGS and any("$" in a or "`" in a for a in args if not a.startswith("-")):
        return f"{name} with a computed name"
    if p == "printf" and any(a.startswith("-v") for a in args):
        return "printf -v"
    if "$" in name or "`" in name or getattr(name, "glob", False) or getattr(name, "brace", False):
        return name  # the shell decides what this runs
    if not args and (name.startswith("~") or (("/" in name or not _on_path(name))
                                              and _path.isdir(_norm(name, cwd)))):
        return name  # zsh auto_cd: a directory named on its own changes into it
    return None


@lru_cache(maxsize=256)
def _on_path(name: str) -> bool:
    return shutil.which(name) is not None


def _moved_write(st: _State, blk: Blk, what: str) -> Block:
    """The block for a git or gh write in a command that changes directory."""
    return blk(OPAQUE, f"this command changes directory ({st.moved}) and runs {what}; the guard judges git and gh "
                       "writes only in the session's working directory", _CD_HINT)


def _branch(ctx: GuardContext, st: _State, d: str) -> Optional[str]:
    if d in st.switched:
        return st.switched[d]

    def read() -> Optional[str]:
        try:
            found = find_checkout(d)
        except _Ambiguous:
            found = None
        if found:
            return head_branch(found[1])
        b = ctx.git(["branch", "--show-current"], d)
        return None if b in (None, "HEAD") else b
    return st.once(("branch", d), read)


def _git_config(ctx: GuardContext, st: _State, d: str, key: str) -> Optional[str]:
    return st.once(("config", d, key), lambda: ctx.git(["config", "--get", key], d))


def _bash(cmd: str, cwd: str, ctx: GuardContext) -> Iterator[Block]:
    blk = _blk(_short(cmd))
    plain = _unshell(_data_only(cmd))
    if "allow-repo" in plain and any(m.group(1) or "--everywhere".startswith(m.group(2))
                                     for m in _ALLOW_EDIT.finditer(plain)):
        yield blk(SELF, "only the operator changes the not-me allow list", "ask the operator to run /not-me:allow-repo")
    if OPERATOR_TOKEN in plain.lower():
        yield blk(SELF, TOKEN_WHY, TOKEN_HINT)
    if LIVE_EPOCH_NAME in plain.lower():
        yield blk(SELF, LIVE_EPOCH_WHY, LIVE_HINT)
    if LIVE_MARKER_NAME in plain.lower():
        yield blk(SELF, LIVE_MARKER_WHY, LIVE_HINT)
    if GRANT_NAME in plain.lower() or any(GRANT_NAME in v for v in _spellings(plain)):
        yield blk(SELF, GRANT_WHY, GRANT_HINT)
    if ALLOWLIST_NAME in cmd.lower() or str(ctx.allowlist_path).lower() in cmd.lower():
        yield blk(SELF, "the not-me allow list is off limits to the agent",
                  "ask the operator to run /not-me:allow-repo, or read it with /not-me:config")
    try:
        parsed = lex(cmd)
    except Unparsed as exc:  # not even self-protection can be checked: blocked whatever rules are on
        yield blk(SELF, f"the guard cannot read this command with confidence: {exc}",
                  "split it into shorter, simpler commands with plain quoting")
        return
    yield from _parsed_checks(parsed, blk)
    st = _State(cwd, parsed, cmd)
    for c in parsed.commands:
        yield from _command(list(c.words), c.raw, st, ctx)


def _parsed_checks(parsed: Parsed, blk: Blk) -> Iterator[Block]:
    """Checks on a lexed command (or shell body) as a whole: here-documents and substitutions."""
    # a here-document anywhere may reach a shell anywhere in the command (cat <<EOF | bash): checked together
    feeds = any(_prog(w) in _FED_PROGS or _INTERP.match(_prog(w)) for c in parsed.commands for w in c.words)
    if feeds and any(_mentions_github(b) for c in parsed.commands for b in c.bodies):
        yield blk(OPAQUE, "a here-document in a command that runs a shell or interpreter mentions gh/git, which "
                          "cannot be checked", "run gh/git directly")
    if any(has_gh_git(_unshell(s)) for s in parsed.subs):
        yield blk(OPAQUE, "gh/git inside $(...), backticks or <(...) cannot be checked",
                  "run gh/git directly, not inside $(...)")


def _spellings(text: str) -> List[str]:
    """text as the shell or a program may end up reading it: as written, with \\x67 / \\147 / \\u0067 escapes
    decoded, with quotes and backslashes removed, with ${...}, $(...), backticks and $'/$" markers dropped, and
    lowercased (git runs as GIT on a case-insensitive file system)."""
    def esc(m: "re.Match[str]") -> str:
        hexa, octal, uni = m.groups()
        code = int(hexa, 16) if hexa else (int(octal, 8) if octal else int(uni, 16))
        return chr(code)
    decoded = _ESC_SEQ.sub(esc, text)
    out = [text, decoded]
    for v in (text, decoded):
        out.append(_unshell(v))
        out.append(_SHELL_PIECES.sub("", v).replace('"', "").replace("'", "").replace("\\", ""))
    return out + [v.lower() for v in out]


def _mentions_any(text: str) -> bool:
    """text names gh, git or github.com in any spelling _spellings knows."""
    return any(_mentions_github(v) for v in _spellings(text))


def _judge_body(body: str, st: _State, ctx: GuardContext, blk: Blk, what: str) -> Iterator[Block]:
    """Judge a string a program runs as a shell command (bash -c, eval, awk system(), sed e, vim !, tar
    --to-command) as a command in its own right: lexed with the guard's lexer and checked command by command in a
    child state. A body nested too deep, one that cannot be lexed, or one whose words still hold $, quotes,
    backslashes or substitutions after lexing is blocked when it mentions gh/git/github in any spelling or the
    whole command could reach the network."""
    hint = "run gh/git directly, with every word spelled out plainly"
    if st.depth >= MAX_BODY_DEPTH:
        yield blk(OPAQUE, f"{what} nests commands more than {MAX_BODY_DEPTH} deep, which cannot be checked", hint)
        return
    risky = _mentions_any(body) or bool(st.find(_NETWORK, st.text))
    try:
        parsed = lex(body)
    except Unparsed as exc:
        if risky:
            yield blk(OPAQUE, f"{what} runs text the guard cannot read with confidence ({exc})", hint)
        return
    residual = bool(parsed.subs) or any(_OBFUSCATED.search(w) for c in parsed.commands for w in c.words)
    if residual and risky:
        yield blk(OPAQUE, f"{what} runs words that still hold $, quotes, backslashes or substitutions after "
                          "unquoting, so what it runs cannot be checked", hint)
        return
    yield from _parsed_checks(parsed, blk)
    inner = _State(st.dir, parsed, body, parent=st)
    for c in parsed.commands:
        yield from _command(list(c.words), c.raw, inner, ctx)


_RUNS_TEXT = {"eval", "source", ".", "sudo", "doas", "xargs", "parallel"}  # run words they are given as a command


def _core(words: List[str]) -> List[str]:
    """The command past leading assignments, keywords and prefix words such as env, nice or timeout."""
    rest = list(words)
    while rest:
        p = _prog(rest[0])
        if _ASSIGN.match(rest[0]) or p in _KEYWORDS:
            rest = rest[1:]
        elif p in _PREFIX_WORDS:
            rest = _drop_prefix(p, rest[1:])
        else:
            break
    return rest


def _drop_prefix(word: str, rest: List[str]) -> List[str]:
    """The command after a prefix word such as env, time, nice or timeout, without its options."""
    takes_value = _PREFIX_WORDS[word]
    while rest and rest[0].startswith("-") and rest[0] != "-":
        a = rest[0]
        if word == "env" and a.startswith("-S") and len(a) > 2:
            rest = [a[2:]] + rest[1:]
        else:
            rest = rest[2:] if a in takes_value else rest[1:]
    if word == "timeout" and rest:
        rest = rest[1:]  # the duration
    return rest


def _command(t: List[str], seg: str, st: _State, ctx: GuardContext) -> Iterator[Block]:
    """Check one simple command given as tokens."""
    blk = _blk(_short(seg))
    prefixed = False
    assigned: List[str] = []
    wrapper = None
    seen: Set[str] = set()
    while t:
        head = _head(t[0])
        if head == "unbuffer":
            wrapper = "unbuffer"
        if head in _PREFIX_WORDS:
            seen.add(head)
        if _ASSIGN.match(t[0]):
            assigned.append(t[0].split("=", 1)[0])
            t = t[1:]
        elif head in _PREFIX_WORDS:
            t = _drop_prefix(head, t[1:])
            prefixed = True
        elif head in _KEYWORDS:
            t = t[1:]
        else:
            break
    if OPERATOR_CONFIRM in assigned:
        yield blk(SELF, f"{OPERATOR_CONFIRM} is the operator's own confirmation", "ask the operator to confirm")
    if not t:
        yield from _token_reads(t, seg, st, ctx, blk)  # a bare redirection such as $(< file)
        return
    if getattr(t[0], "glob", False) or getattr(t[0], "brace", False) or t[0].startswith("="):
        yield blk(OPAQUE, f"the shell expands the command name '{t[0]}', so what it runs cannot be checked",
                  "spell out the command")
        return
    wrapper = wrapper or _pty_wrapper(t, seg, st)
    if wrapper is not None:
        yield from _drives_notme(t, st, blk, f"{wrapper} gives its command a pseudo-terminal, which would pass "
                                 "not-me's human check")
    yield from _primitives(t, seg, st, blk)
    orphan = _orphaning(t, seen, st)
    if orphan is not None:
        yield from _drives_notme(t, st, blk, f"{orphan} runs it outside this session's process tree, which would "
                                 "escape not-me's human check")
    yield from _token_reads(t, seg, st, ctx, blk)
    word, head = t[0], _head(t[0])
    if "$" in word or "`" in word:
        yield blk(OPAQUE, "a command name taken from a variable cannot be checked", "spell out the command")
        return
    notme_args = _notme_args(t)
    if (head in ("gh", "git") or notme_args is not None) and any(getattr(a, "brace", False) for a in t[1:]):
        yield blk(OPAQUE, "the shell expands {a,b} braces in the arguments, so they cannot be checked",
                  "spell out each argument")
    if notme_args is not None:
        yield from _cmd_notme(notme_args, st, ctx, blk)
    unsafe = [a for a in assigned if a not in _SAFE_ENV]
    if unsafe and (head in ("gh", "git") or notme_args is not None):
        if any(a.startswith("GIT_") for a in unsafe):
            yield blk(OPAQUE, "GIT_* variables change where git reads its repo or config",
                      "run git without GIT_* variables")
        else:
            yield blk(OPAQUE, f"{unsafe[0]}=... in front of {_prog(word)} cannot be checked",
                      "run it without the variable")
    if notme_args is not None:
        return
    handler = _COMMANDS.get(head)
    if handler is not None:
        yield from handler(t, seg, st, ctx, blk, unsafe)
    elif word.startswith(("./", "../", ".\\", "..\\")) or word.endswith(SCRIPT_SUFFIXES):
        yield from _script(word, st, blk)
    elif _INTERP.match(head):
        inline = any(a in ("--eval", "--print") or (a.startswith("-") and not a.startswith("--")
                                                     and set(a[1:]) & set("ceEpr")) for a in t[1:])
        if inline and st.once(("github",), lambda: _mentions_github(st.text)):
            yield blk(OPAQUE, f"{head} inline code that runs gh/git or calls GitHub cannot be checked",
                      "run gh/git directly")
    elif head in _HTTP_CLIENTS and _GITHUB.search(seg):
        yield blk(OPAQUE, f"{head} to GitHub bypasses gh's repo checks", "use gh with --repo owner/name")
    elif prefixed and has_gh_git(seg):
        yield blk(OPAQUE, f"cannot tell what '{word}' runs", "run gh/git directly, without the wrapper")
    elif _prog(word) not in _INERT and any(has_gh_git(a) or has_gh_git(_unshell(a)) for a in t):  # man -P, watch
        yield blk(OPAQUE, f"{_prog(word)} may run gh/git with words the guard cannot check",
                  "run gh/git directly, without the wrapper")


def _pty_wrapper(t: List[str], seg: str, st: _State) -> Optional[str]:
    """The name of a wrapper that gives its command a pseudo-terminal, or None."""
    prog, args = _prog(t[0]), t[1:]
    low = [a.lower() for a in args]
    if prog in ("script", "expect", "unbuffer"):
        return prog
    if prog == "socat" and any("pty" in a for a in low):
        return "socat pty"
    if prog == "tmux" and any(a in ("send-keys", "send", "send-key") for a in low):
        return "tmux send-keys"
    if prog == "screen" and "-x" in low and "stuff" in low:
        return "screen -X stuff"
    if prog == "ssh":
        flags = _short_flags([a for a in args if a.startswith("-")])
        dest = next((a for a in _skip_options(args, {"-p", "-l", "-i", "-o", "-F", "-J", "-L", "-R", "-D", "-E",
                                                      "-b", "-c", "-e", "-m", "-O", "-Q", "-S", "-W", "-w"})), "")
        if "t" in flags and _LOCALHOST.match(dest):
            return "ssh -t to this machine"
    if _INTERP.match(prog):
        inline = any(a in ("-", "--eval", "--print") or (a.startswith("-") and not a.startswith("--")
                                                        and set(a[1:]) & set("ceEpr")) for a in args)
        if (inline or "<<" in seg or len(_positionals(args)) == 0) and st.find(_PTY_CODE, st.raw):
            return f"{prog} pty code"
    return None


def _primitives(t: List[str], seg: str, st: _State, blk: Blk) -> Iterator[Block]:
    """Interpreter code (inline or a script file) that forges a terminal or detaches from the session."""
    prog, args = _prog(t[0]), t[1:]
    if not _INTERP.match(prog):
        return
    why = "{} can forge a terminal or leave this session's process tree ({}), which would pass not-me's human check"
    hint = "do this without pty, fork, setsid or daemon code"
    inline = any(a in ("-", "--eval", "--print") or (a.startswith("-") and not a.startswith("--")
                                                    and set(a[1:]) & set("ceEpr")) for a in args)
    pos = _positionals(args)
    if inline or "<<" in seg or not pos:
        m = st.find(_PRIMITIVES, st.raw)
        if m:
            yield blk(SELF, why.format(f"{prog} inline code", m.group(0)), hint)
        return
    if st.moved and "-m" not in args and any(not _path.isabs(a) for a in pos):
        yield blk(SELF, f"{prog} runs a file named by a relative path in a command that changes directory, so the "
                        "guard cannot read it", _CD_HINT)
        return
    for a in pos:  # the script is the first file among them; option values are rarely files
        text = _read_script(_norm(a, st.dir), st)
        m = _PRIMITIVES.search(text) if text is not None else None
        if m:
            yield blk(SELF, why.format(a, m.group(0)), hint)
            return


def _orphaning(t: List[str], seen: Set[str], st: _State) -> Optional[str]:
    """What detaches this command from the session (nohup, setsid, at, a background &, ...), or None."""
    prog, low = _prog(t[0]), [a.lower() for a in t[1:]]
    if seen & _ORPHAN_PREFIX:
        return sorted(seen & _ORPHAN_PREFIX)[0]
    if prog in _ORPHAN_PROGS:
        return prog
    if prog == "launchctl" and {"submit", "bootstrap"} & set(low):
        return "launchctl " + next(a for a in low if a in ("submit", "bootstrap"))
    if prog == "crontab":
        rest = [a for i, a in enumerate(low) if a != "-u" and not (i and low[i - 1] == "-u")]
        if rest != ["-l"]:  # anything but listing installs or edits a crontab
            return "crontab"
    if st.background:
        return "a background &"
    return None


def _expand_home(arg: str, ctx: GuardContext) -> str:
    """arg with $HOME / ${HOME} and $NOT_ME_HOME / ${NOT_ME_HOME} filled in (~ is left to _norm)."""
    for var in ("${HOME}", "$HOME"):
        arg = arg.replace(var, ctx.env.get("HOME") or os.path.expanduser("~"))
    for var in ("${NOT_ME_HOME}", "$NOT_ME_HOME"):
        arg = arg.replace(var, str(ctx.home))
    return arg


def _reaches(arg: str, base: str, guarded: List[_Guarded], recursive: bool,
             ctx: GuardContext) -> Optional[_Guarded]:
    """The guarded file that arg (a path or glob, relative to base) names, or that a directory it names holds
    when recursive; else None. Globs are expanded for real and also matched as a pattern (the file need not
    exist yet); one that visits more than GLOB_LIMIT paths counts as reaching a file its fixed leading directory
    holds. Parts the guard cannot resolve ($VAR, $(...), backticks, {a,b}) count as * and fail closed when the
    rest pins the pattern near a guarded file; with nothing fixed left the argument cannot be placed at all."""
    arg = _expand_home(arg.replace('"', "").replace("'", ""), ctx)
    arg = _HOME_PARAM.sub(lambda m: str(ctx.home) if m.group(1) == "NOT_ME_HOME"
                          else (ctx.env.get("HOME") or os.path.expanduser("~")), arg)
    if _UNRESOLVED.search(arg):
        pattern = _UNRESOLVED.sub("*", arg)
        return _pattern_hit(pattern, base, guarded, recursive) if pattern.strip("*/\\.") else None
    full = _path.join(base, _path.expanduser(arg)) if not _path.isabs(_path.expanduser(arg)) \
        else _path.expanduser(arg)

    def hit(path: str) -> Optional[_Guarded]:
        n = _norm(path, base)
        return next((g for g in guarded if _under(n, g[0]) or (recursive and _under(g[0], n))), None)
    if not _GLOB.search(arg):
        return hit(full)
    try:
        for k, match in enumerate(glob.iglob(full, recursive=True)):
            found = hit(match)
            if found:
                return found
            if k >= GLOB_LIMIT:
                fixed = _PATH_SEP.split(full)
                fixed = fixed[:next(i for i, part in enumerate(fixed) if _GLOB.search(part))]
                top = _norm(_path.sep.join(fixed) or _path.sep, base)
                return next((g for g in guarded if _under(g[0], top)), None)
    except (OSError, ValueError, re.error):
        return guarded[0]  # cannot expand: fail closed
    return _pattern_hit(full, base, guarded, recursive)


def _token_reads(t: List[str], seg: str, st: _State, ctx: GuardContext, blk: Blk) -> Iterator[Block]:
    """Reading, copying or writing commands (and <, > and $(< operands) whose arguments reach the operator token,
    the live-mode epoch or the live marker."""
    prog = _prog(t[0]) if t else ""
    targets = [m.group(1) for m in _IN_REDIRECT.finditer(seg)] + [m.group(1) for m in _OUT_REDIRECT.finditer(seg)]
    if prog in _TOKEN_READERS:
        targets += [a[3:] if prog == "dd" and a.startswith(("if=", "of=")) else a
                    for a in t[1:] if not a.startswith("-")]
    if not targets:
        return
    guarded = st.once(("guarded",), lambda: _guarded(ctx, st.dir, _HOME_GUARDED))
    recursive = prog in _TOKEN_COPIERS or (prog in ("grep", "egrep", "fgrep", "rg") and (
        prog == "rg" or "r" in _short_flags(t[1:]).lower() or "--recursive" in t[1:]))
    for a in targets:
        if st.moved and not _path.isabs(_expand_home(a, ctx)):
            found = next((g for g in guarded if _may_reach_token(a, g[0], recursive)), None)
        else:
            found = _reaches(a, st.dir, guarded, recursive, ctx)
        if found:
            yield blk(SELF, found[2], found[3])
            return


def _may_reach_token(arg: str, token: str, recursive: bool) -> bool:
    """After a directory change the base of a relative path is unknown: could arg name the guarded file `token`,
    or (recursive) a directory that holds it? Judged by its last part against the names on the file's path."""
    last = _PATH_SEP.split(arg.rstrip("/\\"))[-1]
    if "$" in last or "`" in last:
        return True
    if last in ("", ".", "..") or last.startswith("~"):
        return recursive
    names = [p for p in _PATH_SEP.split(token) if p]
    return any(fnmatch.fnmatch(n.lower(), last.lower()) for n in (names if recursive else names[-1:]))


def _drives_notme(t: List[str], st: _State, blk: Blk, why: str) -> Iterator[Block]:
    """A wrapper that hides the caller (a pty, a detached process) may not drive the not-me CLI or its controls."""
    hint = "the operator runs this not-me step in their own terminal"
    if st.moved:
        yield blk(SELF, f"{why}; in a command that changes directory the guard cannot tell which files it runs",
                  _CD_HINT)
        return
    if st.find(_PTY_TRIGGER, st.plain_raw):
        yield blk(SELF, why, hint)
        return
    for a in t:
        if a.startswith("-"):
            continue
        text = _read_script(_norm(a, st.dir), st)
        if text is not None and _PTY_TRIGGER.search(text):
            yield blk(SELF, f"{why} ({a} mentions it)", hint)
            return


def _read_script(path: str, st: Optional[_State] = None) -> Optional[str]:
    """The start of a regular file as text, or None (memoised per call when st is given)."""
    if st is not None:
        return st.once(("read", path), lambda: _read_script(path))
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            return fh.read(SCRIPT_READ_LIMIT).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def _prog(word: str) -> str:
    """Program name for matching: basename, backslashes removed, lowercased, without .exe."""
    low = _base(word.replace("\\", "")).lower()
    return low[:-4] if low.endswith(".exe") else low


def _notme_args(t: List[str]) -> Optional[List[str]]:
    """The arguments of a not-me CLI call (bin/notme, cli/notme.py, a shell or python running it, or
    `python -m notme` / `-m notme.cli`); else None."""
    prog = _prog(t[0])
    if prog in _NOTME_PROGS:
        return t[1:]
    if prog in _SHELLS or prog == "notme-python" or _INTERP.match(prog):
        python = prog == "notme-python" or prog.startswith("py")
        skip = False
        for k, a in enumerate(t[1:], 1):
            if skip:  # the value of a python option such as -W ignore or -X utf8
                skip = False
                continue
            if python and a == "--check-hash-based-pycs":
                skip = True
                continue
            if python and a.startswith("-") and not a.startswith("--") and len(a) > 1:
                # a short option cluster such as -W ignore, -Wignore, -X utf8, -bW x, -Bm notme.cli, -Ic code
                flags = a[1:]
                i = next((j for j, c in enumerate(flags) if c in _PY_VALUE_OPTS + "cm"), None)
                if i is None:
                    continue
                opt, rest = flags[i], flags[i + 1:]
                if opt in _PY_VALUE_OPTS:
                    skip = not rest  # the value is the next word unless it is joined on
                    continue
                if opt == "c":
                    return None  # inline code: the interpreter checks handle it
                module = rest or (t[k + 1] if k + 1 < len(t) else "")
                return (t[k + 1:] if rest else t[k + 2:]) if _is_notme_module(module) else None
            if a.startswith("-"):
                module = a[2:] if a.startswith("-m") and len(a) > 2 else (
                    t[k + 1] if a == "-m" and k + 1 < len(t) else None)
                if module is not None and _is_notme_module(module):
                    return t[k + 1:] if len(a) > 2 else t[k + 2:]
                if a in ("-c", "-m", "-e", "--eval"):
                    return None  # inline code: the interpreter checks handle it
                continue
            return _notme_args(t[k:]) if _prog(a) in _NOTME_PROGS | {"notme-python"} else None
    return None


def _is_notme_module(name: str) -> bool:
    """`-m` names the not-me package or one of its modules (notme, notme.cli, ...), in any case or quoting."""
    low = _unshell(name).lower()
    return low == "notme" or low.startswith("notme.")


def _worktrees(ctx: GuardContext) -> List[str]:
    """The session checkout and every worktree of its repo, found without git."""
    dirs = [ctx.root or ctx.cwd]
    common = ctx.common_dir
    if common is not None:
        if Path(common).name == ".git":
            dirs.append(str(Path(common).parent))
        try:
            entries = list((Path(common) / "worktrees").iterdir())
        except OSError:
            entries = []
        for e in entries:
            try:
                dirs.append(_path.dirname((e / "gitdir").read_text(encoding="utf-8").strip()))
            except OSError:
                continue
    return [_norm(d, "/") for d in dirs if d]


def _cmd_notme(args: List[str], st: _State, ctx: GuardContext, blk: Blk) -> Iterator[Block]:
    """Self-protection for the not-me CLI: it trusts its project, home and operator, so nothing may re-point them."""
    hint = "run notme from the session checkout with no overrides"
    plain = [_unshell(a) for a in args]
    if any(a.startswith("--") and len(a) > 2 and "--project-dir".startswith(a.split("=", 1)[0]) for a in plain):
        yield blk(SELF, "notme --project-dir points the CLI at another project", hint)
    m = st.find(_TRUST_ENV, st.plain_text) or st.find(_TRUST_UNSET, st.plain_text)
    if m:
        yield blk(SELF, f"{m.group(1)} changes which project, home or identity notme trusts", hint)
    if st.moved:
        yield blk(SELF, f"notme runs in a command that changes directory ({st.moved}), so the guard cannot tell "
                        "which checkout it runs in", "run cd in a separate Bash call, then run notme")
    elif not st.once(("in-checkout",), lambda: any(_under(_norm(st.dir, "/"), w) for w in _worktrees(ctx))):
        yield blk(SELF, "notme runs outside the session checkout", hint)
    pos = [a for a in plain if not a.startswith("-")]
    raw_pos = [r for r, a in zip(args, plain) if not a.startswith("-")]  # paths keep their backslashes
    if pos[:1] == ["allow-repo"] and pos[1:2] != ["list"]:
        if not ({"--help", "-h"} & set(plain)):
            yield blk(SELF, "only the operator changes the not-me allow list",
                      "ask the operator to run /not-me:allow-repo")
    if "pause" in plain:
        rest = plain[plain.index("pause") + 1:]
        if rest not in _PAUSE_OK:
            yield blk(SELF, "only a person turns a not-me pause off", "leave the pause for the operator")
    if "mode" in pos and not ({"--help", "-h"} & set(plain)):
        # anything but `mode show` or `mode dry-run` (live, an abbreviation, a typo) is the person's switch
        sub = pos[pos.index("mode") + 1:pos.index("mode") + 2]
        if not sub or sub[0] not in _MODE_OK:
            yield blk(SELF, "only a person turns live mode on",
                      LIVE_HINT)
    if pos[:2] == ["git", "worktree-add"]:
        target = _norm(raw_pos[2], st.dir) if len(raw_pos) > 2 else None
        base = _norm(_path.join(str(ctx.common_dir), "not-me", "worktrees"), "/") if ctx.common_dir else None
        if target is None or base is None or target == base or not _under(target, base):
            yield blk(SELF, "notme git worktree-add may only create worktrees under <git common dir>/not-me/worktrees/",
                      "let notme pick the worktree path")


def _script(path: str, st: _State, blk: Blk) -> Iterator[Block]:
    if st.moved and not _path.isabs(path):
        yield blk(OPAQUE, f"{path} is a relative path in a command that changes directory ({st.moved}), so the "
                          "guard cannot tell which file runs", _CD_HINT)
        return
    text = _read_script(_norm(path, st.dir), st)
    if text is None:
        yield blk(OPAQUE, f"cannot read {path} to check what it runs", "run its commands directly")
        return
    m = _PRIMITIVES.search(text)
    if m:
        yield blk(SELF, f"{path} can forge a terminal or leave this session's process tree ({m.group(0)}), which "
                        "would pass not-me's human check", "do this without pty, fork, setsid or daemon code")
    m = _SCRIPT_SELF.search(text)
    if m:
        yield blk(SELF, f"{path} runs the not-me CLI or touches its controls ({m.group(0)}), which cannot be "
                        "checked from here", "run the notme command directly")
    if _mentions_github(text):
        yield blk(OPAQUE, f"{path} runs gh/git or calls GitHub, which cannot be checked from here",
                  "run its gh/git commands directly")


_SHELL_VALUE_OPTS = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
_FISH_BODY_OPTS = ("--command", "--init-command", "-C")  # fish: options whose value is a command


def _shell_bodies(head: str, args: List[str]) -> Tuple[Optional[List[str]], Optional[str]]:
    """(command strings the shell runs from its arguments, or None when it runs none; the script it runs, or
    None). -c makes the first word after the options the command; fish also takes --command and -C values."""
    bodies: List[str] = []
    c_mode = False
    i = 0
    while i < len(args) and args[i].startswith(("-", "+")) and args[i] not in ("-", "--"):
        a = args[i]
        name, eq, val = a.partition("=")
        if head == "fish" and (name in _FISH_BODY_OPTS or (a.startswith("-C") and not a.startswith("--"))):
            if eq:
                bodies.append(val)
            elif a.startswith("-C") and len(a) > 2:
                bodies.append(a[2:])
            elif i + 1 < len(args):
                bodies.append(args[i + 1])
                i += 1
        elif not a.startswith("--") and "c" in a[1:]:
            c_mode = True
        i += 2 if a in _SHELL_VALUE_OPTS else 1
    if i < len(args) and args[i] == "--":
        i += 1
    if c_mode:
        bodies += args[i:i + 1]
    if c_mode or bodies:
        return bodies, None
    return None, (args[i] if i < len(args) else None)


def _cmd_shell(t, seg, st, ctx, blk, gitenv):
    head = _head(t[0])
    args = t[1:]
    if head in _SHELLS:
        bodies, script = _shell_bodies(head, args)
        for body in bodies or []:
            yield from _judge_body(body, st, ctx, blk, f"{head} -c")
        if bodies is not None:
            return
    else:  # source and .
        script = next((a for a in args if a != "--"), None)
    if script is None:  # commands come from its input: a here-document (checked with the command), a file or a pipe
        redirect = [m.group(1) for m in _IN_REDIRECT.finditer(seg)]
        if redirect:
            script = redirect[-1]
        elif "<<" not in seg:
            yield blk(OPAQUE, f"{head} reads commands from its input, which cannot be checked",
                      "run the commands directly, or put them in a script file")
            return
    if script is not None:
        yield from _script(script, st, blk)


def _cmd_eval(t, seg, st, ctx, blk, gitenv):
    # eval joins its words with spaces and runs the result as a command
    yield from _judge_body(" ".join(t[1:]), st, ctx, blk, "eval")


def _wrapped(takes_value: Set[str], stop: Tuple[str, ...] = ()):
    """Handler for xargs, parallel and sudo: check the command they run."""
    def handler(t, seg, st, ctx, blk, gitenv):
        if st.moved and has_gh_git(_unshell(seg)):
            yield _moved_write(st, blk, f"gh/git through {_prog(t[0])}")
            return
        if _prog(t[0]) in ("xargs", "parallel") and any(has_gh_git(a) for a in t[1:]):
            yield blk(OPAQUE, f"{_prog(t[0])} adds words from its input to gh/git, so they cannot be checked",
                      "run gh/git directly with every argument spelled out")
            return
        rest = _skip_options(t[1:], takes_value)
        for k, a in enumerate(rest):
            if a in stop:
                rest = rest[:k]
                break
        if _prog(t[0]) in ("xargs", "parallel"):
            core = _core(rest)
            p = _prog(core[0]) if core else ""
            if not core and _prog(t[0]) == "parallel":
                yield blk(OPAQUE, "parallel with no command runs its input lines as commands, which cannot be checked",
                          "run the commands directly")
                return
            if p in _SHELLS or p in _RUNS_TEXT or _INTERP.match(p):
                yield blk(OPAQUE, f"{_prog(t[0])} runs {p} with words from its input, which cannot be checked",
                          "run the command directly with every word spelled out")
                return
        if rest:
            yield from _command(rest, " ".join(rest), st, ctx)
    return handler


def _cmd_find(t, seg, st, ctx, blk, gitenv):
    if st.moved and has_gh_git(_unshell(seg)):
        yield _moved_write(st, blk, "gh/git through find")
        return
    for k, a in enumerate(t):
        if a in _FIND_EXEC:
            sub: List[str] = []
            for w in t[k + 1:]:
                if w in (";", "\\;", "+", "\\"):
                    break
                sub.append(w)
            if sub:
                yield from _command(sub, " ".join(sub), st, ctx)


def _cmd_export(t, seg, st, ctx, blk, gitenv):
    for w in t[1:]:
        if w.split("=", 1)[0] == OPERATOR_CONFIRM:
            yield blk(SELF, f"{OPERATOR_CONFIRM} is the operator's own confirmation", "ask the operator to confirm")
        if w.startswith("GIT_"):
            yield blk(OPAQUE, f"{t[0]} {w} changes where git reads its repo or config", "do not set GIT_* variables")


def _cmd_rm(t, seg, st, ctx, blk, gitenv):
    args = t[1:]
    flags = _short_flags(args)
    if not ("r" in flags or "R" in flags or "--recursive" in args):
        return
    root = _norm(ctx.root, "/") if ctx.root else None
    for target in _positionals(args):
        if st.moved and not _path.isabs(target):  # unknown base: could it name the checkout or a directory above?
            last = _PATH_SEP.split(target.rstrip("/\\"))[-1]
            if root is not None and (last in ("", ".", "..") or last.startswith(("~", "$", "`"))
                                     or fnmatch.fnmatch(_path.basename(root).lower(), last.lower())):
                yield blk("destructive_git", f"rm -r on {target} in a command that changes directory may delete "
                          "the checkout", "delete by absolute path, or run cd in a separate Bash call")
            continue
        full = _norm(target, st.dir)
        hits_root = root is not None and (_under(root, full) or (target in ("*", ".*") and st.dir == root))
        if ".git" in [p.lower() for p in _PATH_SEP.split(target)] or hits_root:
            yield blk("destructive_git", f"rm -r on {target} would delete the checkout or its git data",
                      "delete specific files instead; ask the operator to remove a checkout")


# --- git -------------------------------------------------------------------


@dataclass
class _GitCall:
    ctx: GuardContext
    st: _State
    blk: Blk
    d: str
    sub: str
    args: List[str] = field(default_factory=list)
    has_c: bool = False


def _cmd_git(t, seg, st, ctx, blk, gitenv):
    d = st.dir
    c_values: List[str] = []
    i = 1
    while i < len(t) and t[i].startswith("-"):
        a = t[i]
        if a == "-C":
            d = _norm(t[i + 1] if i + 1 < len(t) else ".", d)
            i += 2
        elif a == "-c":
            c_values.append(t[i + 1] if i + 1 < len(t) else "")
            i += 2
        elif a in _GIT_GLOBALS:
            i += 1
        else:
            yield blk(OPAQUE, f"git {a.split('=')[0]} cannot be checked", "use git -C <checkout> and no other "
                                                                          "options before the subcommand")
            return
    sub, args = (t[i] if i < len(t) else ""), t[i + 1:]
    known = sub in _GIT_READ_ONLY or sub in _GIT_LOCAL or sub in _GIT_RULES or sub in _GIT_LISTING
    if not known:
        yield blk(OPAQUE, f"git {sub} is not a subcommand the guard knows (an alias, extension or command that can "
                          "run programs)", "use a plain git command the guard knows, or ask the operator")
        return
    bad_key = next((v for v in c_values if not _safe_git_key(v.split("=", 1)[0])), None)
    if bad_key is not None:
        yield blk(OPAQUE, f"git -c {_short(bad_key, 40)} can run programs or redirect git",
                  "run git without -c, or only with keys such as user.name or color.ui")
        return
    env = _exec_env(st)
    if env:
        yield blk(OPAQUE, f"{env} makes git run another program or read another repo", "run git without it")
        return
    bad = _git_exec_option(sub, args)
    if bad:
        yield blk(OPAQUE, f"git {sub} {bad} runs a program or reaches past the guard",
                  "run git without that option")
        return
    if _git_writes(sub, args):
        if st.moved:
            yield _moved_write(st, blk, f"git {sub}".strip())
            return
        if "GIT_" in st.plain_text:
            yield blk(OPAQUE, "GIT_* variables in a command with a git write can change where git reads its repo "
                              "or config", "run git without GIT_* variables")
    elif sub in _GIT_READ_ONLY:
        return
    handler = _GIT_RULES.get(sub)
    if handler is not None:
        yield from handler(_GitCall(ctx, st, blk, d, sub, args, bool(c_values)))


def _safe_git_key(key: str) -> bool:
    key = key.lower()
    return key in _GIT_SAFE_KEYS or key.startswith(_GIT_SAFE_KEY_PREFIXES)


def _exec_env(st: _State) -> Optional[str]:
    """How the command sets a variable that can make git or gh run another program or read other config
    (assignment, export, read, printf -v, mapfile, declare, local, unset, a computed name), or None."""
    return st.once(("exec-env",), lambda: _env_setting(st.commands, st.text, st, 0))


def _env_setting(commands: List[SimpleCommand], text: str, st: _State, depth: int) -> Optional[str]:
    """_exec_env over these commands and their source text; eval bodies and sourced files are read the same way."""
    if depth > MAX_BODY_DEPTH:
        return f"eval or source nested more than {MAX_BODY_DEPTH} deep"
    m = _ENV_DEFAULT.search(text) or _ENV_ARITH.search(text)
    if m:
        return _short(m.group(0), 40)
    for c in commands:
        found = _env_words(c.words, st, depth)
        if found:
            return found
    return None


def _env_words(words: List[str], st: _State, depth: int) -> Optional[str]:
    for w in words:
        m = _ENV_ASSIGN.match(w)
        if m and not (m.group(1) in _SAFE_GIT_ENV or m.group(2) in _SAFE_EXEC_ENV.get(m.group(1), ())):
            return f"{m.group(1)}={_short(m.group(2), 30)}"
    core = _core(words)
    name, args = (_prog(core[0]), core[1:]) if core else ("", [])
    found = _allexport(name, args)
    if found:
        return found
    if name in ("declare", "typeset", "local") and any(
            a.startswith("-") and not a.startswith("--") and "n" in a[1:] for a in args):
        return f"{name} -n (a reference can set any variable)"
    if name == "eval":
        body = " ".join(args)
        try:
            parsed = lex(body)
        except Unparsed:
            return "eval with text the guard cannot read"
        for c in parsed.commands:
            for w in c.words:
                left = w.split("=", 1)[0]
                if "=" in w and ("$" in left or "`" in left):
                    return "eval with a computed name"
        return _env_setting(parsed.commands, parsed.code, st, depth + 1)
    if name in ("source", "."):
        path = next((a for a in args if a != "--"), None)
        if path is None:
            return f"{name} of standard input"
        if st.moved and not _path.isabs(path):
            return f"{name} {path} in a command that changes directory"
        text = _read_script(_norm(path, st.dir), st)
        if text is None:
            return f"{name} {path}, which cannot be read"
        try:
            parsed = lex(text)
        except Unparsed:
            return f"{name} {path}, which cannot be read with confidence"
        return _env_setting(parsed.commands, parsed.code, st, depth + 1)
    setters = [_prog(w) for w in words if _prog(w) in _ENV_SETTERS]
    if not setters:
        return None
    for w in words:
        if _ENV_WORD.fullmatch(w):
            return f"{setters[0]} {w}"
    if "printf" in setters and any(w.startswith("-v") for w in words):
        return "printf -v"
    if set(setters) - {"printf", "for", "select", "env"} and any(
            ("$" in w.split("=", 1)[0] or "`" in w.split("=", 1)[0]) for w in words if not w.startswith("-")):
        return f"{setters[0]} with a computed name"
    return None


_EXPORT_OPTS = {"allexport", "keyword"}  # set -o names that export every assignment (or take them from arguments)


def _allexport(name: str, args: List[str]) -> Optional[str]:
    """`set -a`, `set -k`, `set -o allexport`, `setopt allexport`: later assignments reach git and gh."""
    if name == "set":
        for k, a in enumerate(args):
            if not a.startswith("-") or a.startswith("--"):
                continue
            if set(a[1:]) & {"a", "k"}:
                return f"set {a}"
            nxt = args[k + 1].lower() if k + 1 < len(args) else ""
            if "o" in a[1:] and nxt in _EXPORT_OPTS:
                return f"set -o {nxt}"
    if name in ("setopt", "unsetopt"):  # zsh: setopt all_export, unsetopt no_all_export
        prefix = "" if name == "setopt" else "no"
        for a in args:
            if a.lower().replace("_", "") in {prefix + o for o in _EXPORT_OPTS}:
                return f"{name} {a}"
    return None


def _git_exec_option(sub: str, args: List[str]) -> Optional[str]:
    """The first option that lets this git call run a program or escape the read allow-list, or None."""
    if sub in _GIT_READ_ONLY:
        for a in args:
            if a == "--":
                break
            if sub == "ls-remote" and not a.startswith("--") and "u" in a:
                return a  # ls-remote -u is --upload-pack
            if not a.startswith("-") or a == "-" or a in _GIT_READ_OPTS:
                continue
            if a.startswith("--"):
                if "=" in a and a.split("=", 1)[0] in _GIT_READ_VALUE_OPTS:
                    continue
                return a
            if _git_short_ok(sub, a):
                continue
            return a
        return None
    exact = _GIT_WRITE_LONG.get(sub)
    for a in args:
        if a == "--":
            break
        name = a.split("=", 1)[0]
        if exact is not None and a.startswith("--") and name not in exact:
            return a  # git accepts any unambiguous prefix of a long option: only exact known names pass
        abbrev = a.startswith("--") and len(name) > 2
        for opt in _GIT_EXEC_OPTS.get(sub, ()):
            long_hit = opt.startswith("--") and abbrev and opt.startswith(name)
            short_hit = not opt.startswith("--") and a.startswith("-") and not a.startswith("--") and opt[1] in a
            if long_hit or short_hit:
                return a
        strategy = None
        if abbrev and "--strategy".startswith(name) and "=" in a:
            strategy = a.split("=", 1)[1]
        elif (abbrev and "--strategy".startswith(name)) or (a == "-s" and sub in ("merge", "pull", "rebase")):
            nxt = args[args.index(a) + 1:args.index(a) + 2]
            strategy = nxt[0] if nxt else ""
        elif a.startswith("-s") and len(a) > 2 and not a.startswith("--") and sub in ("merge", "pull", "rebase"):
            strategy = a[2:]
        if strategy is not None and strategy not in _MERGE_STRATEGIES:
            return a
    return None


_PICKAXE_SUBS = {"log", "diff", "show", "whatchanged", "diff-tree", "diff-files", "diff-index", "blame", "annotate"}


def _git_short_ok(sub: str, a: str) -> bool:
    """A short option on a read: a count (-5, -n5, -U3), a pickaxe or range value where it takes one (-Sfoo,
    -L1,9:f), or a cluster of letters that are each safe (so -nO'cmd' fails on the O)."""
    body = a[1:]
    if re.fullmatch(r"\d+|[nUABCMm]\d+%?", body):
        return True
    if sub in _PICKAXE_SUBS and re.fullmatch(r"[SGL].+", body, re.S):
        return True
    return set(body) <= _GIT_SAFE_SHORT


def _git_writes(sub: str, args: List[str]) -> bool:
    """True unless this git subcommand only reads (a listing form such as `git branch -a` counts as a read)."""
    if sub in _GIT_READ_ONLY:
        return False
    if sub == "remote":
        return not _remote_lists(args)
    if sub == "config":
        return not _config_reads(args)
    if sub not in _GIT_LISTING:
        return True
    words, every = _GIT_LISTING[sub]
    if every:
        return not all(a in words for a in args)
    return (args[0] if args else "") not in words


def _remote_lists(args: List[str]) -> bool:
    """git remote [-v], git remote get-url [--push|--all] <name>, git remote show [-n] <name>."""
    rest = [a for a in args if a not in ("-v", "--verbose")]
    if not rest:
        return True
    verb, more = rest[0], rest[1:]
    opts = {"get-url": {"--push", "--all"}, "show": {"-n"}}.get(verb)
    if opts is None:
        return False
    names = [a for a in more if not a.startswith("-")]
    return all(a in opts for a in more if a.startswith("-")) and (len(names) == 1 or verb == "show")


_CONFIG_READ_OPTS = {"--get", "--get-all", "--get-regexp", "--list", "-l", "--show-origin", "--show-scope", "--global",
                     "--local", "--system", "--worktree", "-z", "--null", "--name-only", "--includes",
                     "--no-includes", "--bool", "--int", "--path"}


def _config_reads(args: List[str]) -> bool:
    """git config --get k, --get-all k, --get-regexp re, --list, `get k`, `list`, or a bare key."""
    opts = [a for a in args if a.startswith("-")]
    pos = [a for a in args if not a.startswith("-")]
    if not all(a in _CONFIG_READ_OPTS or a.startswith(("--type=", "--default=")) for a in opts):
        return False
    if {"--list", "-l"} & set(opts) or pos[:1] == ["list"]:
        return len(pos) <= (1 if pos[:1] == ["list"] else 0)
    if pos[:1] == ["get"]:
        pos = pos[1:]
    limit = 2 if "--get-regexp" in opts else 1
    return 1 <= len(pos) <= limit


def _git_switch(c: _GitCall) -> Iterator[Block]:
    """Track the branch a checkout/switch moves to; unknown outcomes fall back to reading HEAD."""
    c.st.switched.pop(c.d, None)
    c.st.memo.pop(("branch", c.d), None)
    new = [c.args[k + 1] for k, a in enumerate(c.args[:-1]) if a in ("-b", "-B", "-c", "-C")]
    pos = _positionals(c.args)
    if new:
        c.st.switched[c.d] = new[0]
    elif c.sub == "switch" and pos:
        c.st.switched[c.d] = pos[0]
    elif len(pos) == 1 and "--" not in c.args and not os.path.exists(os.path.join(c.d, pos[0])):
        c.st.switched[c.d] = pos[0]
    return iter(())


def _git_send_pack(c: _GitCall) -> Iterator[Block]:
    yield c.blk("other_repo", "git send-pack cannot be checked", "use git push")


def _git_subtree(c: _GitCall) -> Iterator[Block]:
    if "push" in c.args:
        yield c.blk("other_repo", "git subtree push cannot be checked", "push the subtree repo from its own checkout")


def _git_commit(c: _GitCall) -> Iterator[Block]:
    b = _branch(c.ctx, c.st, c.d)
    if b in c.ctx.protected_branches:
        yield c.blk("protected_branches", f"commits on the protected branch {b} are not allowed",
                    "create a feature branch first (git switch -c <type>/<topic>) and commit there")
    origin = c.st.once(("origin", c.d), lambda: c.ctx.git(["remote", "get-url", "origin"], c.d))
    slug = parse_origin(origin)
    if slug:
        yield from _check_target(c.ctx, slug, c.blk)
    else:
        yield _unknown_target(c.blk, "the checkout has no github origin", "commit in a checkout of an allowed repo")


def _git_remote(c: _GitCall) -> Iterator[Block]:
    if c.args and c.args[0] in ("add", "set-url", "rename"):
        yield c.blk("other_repo", f"git remote {c.args[0]} can redirect pushes", "ask the operator to change remotes")


def _git_config_cmd(c: _GitCall) -> Iterator[Block]:
    """Reads pass; a write may only set or unset a key from the safe list, in the repo or global file."""
    if _config_reads(c.args):
        return
    opts = [a for a in c.args if a.startswith("-")]
    pos = [a for a in c.args if not a.startswith("-")]
    if pos[:1] in (["set"], ["unset"]):
        pos = pos[1:]
    ok_opts = all(a in ("--global", "--local", "--add", "--unset", "--unset-all", "--replace-all", "--bool",
                        "--int", "--path") or a.startswith("--type=") for a in opts)
    if not (ok_opts and pos and _safe_git_key(pos[0])):
        what = pos[0] if pos else " ".join(c.args)
        yield c.blk("other_repo", f"git config {_short(what, 40)} can run programs or redirect pushes",
                    "ask the operator to change git config")


def _git_move(c: _GitCall) -> Iterator[Block]:
    """merge, rebase and reset while on a protected branch."""
    if any(a in ("--abort", "--quit") for a in c.args):
        return
    b = _branch(c.ctx, c.st, c.d)
    if b not in c.ctx.protected_branches:
        return
    if c.sub != "reset":
        yield c.blk("protected_branches", f"git {c.sub} on the protected branch {b}",
                    "switch to a feature branch first; merging into it is a human action")
        return
    if "--hard" in c.args:
        yield c.blk("destructive_git", f"git reset --hard on the protected branch {b} discards work",
                    "ask the operator to reset protected branches by hand")
    modes = {"--hard", "--soft", "--mixed", "--keep", "--merge"} & set(c.args)
    pos = [a for a in c.args if not a.startswith("-")]
    if modes or (pos and "--" not in c.args):
        yield c.blk("protected_branches", f"git reset on the protected branch {b}", "switch to a feature branch first")


def _git_branch(c: _GitCall) -> Iterator[Block]:
    flags = _short_flags(c.args)
    delete = "D" in flags or "d" in flags or "--delete" in c.args
    force = "D" in flags or "f" in flags or "--force" in c.args
    names = _positionals(c.args)
    if force and any(n in c.ctx.protected_branches for n in (names if delete else names[:1])):
        what = "force-deletes" if delete else "force-moves"
        yield c.blk("destructive_git", f"git branch {what} a protected branch", "leave protected branches alone")


def _git_clean(c: _GitCall) -> Iterator[Block]:
    flags = _short_flags(c.args) + ("f" if "--force" in c.args else "")
    if "f" in flags and ("x" in flags or "X" in flags):
        yield c.blk("destructive_git", "git clean -x deletes ignored files such as local config and caches",
                    "use git clean -fd, or delete specific files")


def _git_rewrite(c: _GitCall) -> Iterator[Block]:
    yield c.blk("destructive_git", f"git {c.sub} rewrites history", "ask the operator to rewrite history by hand")


def _git_update_ref(c: _GitCall) -> Iterator[Block]:
    if "-d" in c.args or "--delete" in c.args:
        yield c.blk("destructive_git", "git update-ref -d deletes a ref", "ask the operator to delete refs by hand")


_PUSH_LONG = {"--set-upstream", "--force", "--force-with-lease", "--no-force-with-lease", "--force-if-includes",
              "--no-force-if-includes", "--delete", "--mirror", "--all", "--branches", "--tags", "--follow-tags",
              "--no-follow-tags", "--verify", "--no-verify", "--dry-run", "--porcelain", "--quiet", "--verbose",
              "--progress", "--no-progress", "--atomic", "--no-atomic", "--signed", "--no-signed", "--prune",
              "--thin", "--no-thin", "--ipv4", "--ipv6", "--repo", "--push-option", "--receive-pack", "--exec",
              "--recurse-submodules", "--no-recurse-submodules"}


def _push_default_target(c: _GitCall, current: Optional[str], remote: Optional[str]) -> Iterator[Block]:
    """A push with no refspec goes where the repo config says: remote.<name>.push, push.default and the
    branch's upstream. Block when that is a protected branch or cannot be told."""
    ctx, st, blk, d = c.ctx, c.st, c.blk, c.d
    if current is None:
        yield blk("protected_branches", "git push with no branch named, on a detached HEAD, cannot be checked",
                  "name the branch: git push origin <branch>")
        return
    name = remote if remote and parse_origin(remote) is None and "://" not in remote else None
    name = name or _git_config(ctx, st, d, f"branch.{current}.pushRemote") \
        or _git_config(ctx, st, d, "remote.pushDefault") or _git_config(ctx, st, d, f"branch.{current}.remote") \
        or "origin"
    if _git_config(ctx, st, d, f"remote.{name}.push"):
        yield blk("protected_branches", f"remote.{name}.push decides what a bare git push sends",
                  "name the branch: git push origin <branch>")
        return
    keys = st.once(("remote-keys", d, name), lambda: ctx.git(
        ["config", "--get-regexp", "^remote\\." + re.escape(name) + "\\.(push|mirror)$"], d)) or ""
    for line in keys.splitlines():
        key, _, value = line.strip().partition(" ")
        if key.lower().endswith(".push") or value.strip().lower() not in ("false", "no", "off", "0"):
            yield blk("protected_branches", f"{key} decides what a bare git push sends",
                      "name the branch: git push origin <branch>")
            return
    mode = (_git_config(ctx, st, d, "push.default") or "simple").strip().lower()
    if mode not in ("simple", "current", "upstream", "tracking", "nothing"):
        yield blk("protected_branches", f"push.default={mode} can push other branches",
                  "name the branch: git push origin <branch>")
        return
    upstream = _git_config(ctx, st, d, f"branch.{current}.merge") if mode != "current" else None
    up = (upstream or "").strip()
    up = up[len("refs/heads/"):] if up.startswith("refs/heads/") else up
    if up in ctx.protected_branches:
        yield blk("protected_branches", f"{current} pushes to its upstream, the protected branch {up}",
                  "name the branch: git push origin <branch>")


def _git_push(c: _GitCall) -> Iterator[Block]:
    ctx, st, blk, d = c.ctx, c.st, c.blk, c.d
    force = delete = mirror = all_ = False
    positional: List[str] = []
    repo_opt: Optional[str] = None
    for a in c.args:
        name = a.split("=", 1)[0]
        if a.startswith("--") and name not in _PUSH_LONG:  # also git's abbreviations, such as --rec or --for
            yield blk("other_repo", f"git push {name}: option not known to the guard", "spell out a plain option")
            return
        if a.startswith("--repo="):
            repo_opt = a[len("--repo="):]
        elif a in ("-o", "--push-option", "--receive-pack", "--exec", "--repo") or \
                a.startswith(("--receive-pack=", "--exec=", "--push-option=")):
            yield blk("other_repo", f"git push {a.split('=')[0]}: option not supported by the guard",
                      "push without that option")
            return
        elif name == "--recurse-submodules" and a.split("=", 1)[-1] not in ("no", "check"):
            yield blk("other_repo", "git push --recurse-submodules also pushes submodule repos",
                      "push without it")
            return
        elif a.startswith("--force"):
            force = True
        elif a == "--delete":
            delete = True
        elif a == "--mirror":
            mirror = True
        elif a in ("--all", "--branches"):
            all_ = True
        elif a.startswith("--"):
            continue
        elif a.startswith("-") and len(a) > 1:
            force = force or "f" in a[1:]
            delete = delete or "d" in a[1:]
        else:
            positional.append(a)
    remote = repo_opt if repo_opt is not None else (positional[0] if positional else None)
    refspecs = positional if repo_opt is not None else positional[1:]

    if force or any(r.startswith("+") for r in refspecs):
        yield blk("force_push", "force pushes rewrite published history",
                  "push new commits on top instead; ask the operator if a rewrite is really needed")
    if delete or any(r.startswith(":") for r in refspecs):
        yield blk("force_push", "this push deletes a remote branch", "leave remote branches for a person to delete")
    if mirror:
        yield blk("destructive_git", "git push --mirror overwrites every remote ref", "push one feature branch")
    if all_ and ctx.protected_branches:
        yield blk("protected_branches", "git push --all would push protected branches", "push one feature branch")
    recurse = (_git_config(ctx, st, d, "push.recurseSubmodules") or "").strip().lower()
    if recurse not in ("", "no", "check", "false"):
        yield blk("other_repo", f"push.recurseSubmodules={recurse} also pushes submodule repos",
                  "ask the operator to unset push.recurseSubmodules")
    current = _branch(ctx, st, d)
    for r in refspecs or [""]:
        s = r.lstrip("+")
        dst = s.split(":", 1)[1] if ":" in s else s
        dst = (current or "") if dst in ("", "HEAD", "@") else dst
        dst = dst[len("refs/heads/"):] if dst.startswith("refs/heads/") else dst
        if dst in ctx.protected_branches:
            yield blk("protected_branches", f"pushes to the protected branch {dst} are not allowed",
                      "push a feature branch and open a pull request")
            break
    if not refspecs and not (mirror or all_):
        yield from _push_default_target(c, current, remote)

    if c.has_c:
        yield blk("other_repo", "git -c with push can rewrite the push URL", "push without -c")
        return
    if remote is not None:
        url_repo = parse_origin(remote)
        if url_repo:
            yield from _check_target(ctx, url_repo, blk)
            return
        if "://" in remote or _SCP_REMOTE.match(remote) or remote.startswith(("/", "./", "../")):
            yield blk("other_repo", f"git push to {remote} cannot be checked",
                      "push to a named remote that points at an allowed github repo")
            return
    else:
        b = current
        remote = (b and _git_config(ctx, st, d, f"branch.{b}.pushRemote")) \
            or _git_config(ctx, st, d, "remote.pushDefault") \
            or (b and _git_config(ctx, st, d, f"branch.{b}.remote")) or "origin"
    out = st.once(("push-urls", d, remote), lambda: ctx.git(["remote", "get-url", "--push", "--all", remote], d))
    urls = [u.strip() for u in (out or "").splitlines() if u.strip()]
    if not urls:
        yield blk("other_repo", f"remote '{remote}' has no push URL here",
                  "push to a remote that exists and points at an allowed repo")
    for url in urls:
        slug = parse_origin(url)
        if slug:
            yield from _check_target(ctx, slug, blk)
        else:
            yield _unknown_target(blk, f"push URL {url} is not a github repo",
                                  "push to a remote that points at an allowed github repo")


_GIT_RULES: Dict[str, Callable[[_GitCall], Iterator[Block]]] = {
    "checkout": _git_switch, "switch": _git_switch, "send-pack": _git_send_pack, "subtree": _git_subtree,
    "push": _git_push, "commit": _git_commit, "remote": _git_remote, "config": _git_config_cmd,
    "merge": _git_move, "rebase": _git_move, "reset": _git_move, "branch": _git_branch, "clean": _git_clean,
    "filter-branch": _git_rewrite, "filter-repo": _git_rewrite, "update-ref": _git_update_ref,
}

# --- gh --------------------------------------------------------------------


def _cmd_gh(t, seg, st, ctx, blk, gitenv):
    group = t[1] if len(t) > 1 else ""
    env = _exec_env(st)
    if env:
        yield blk(OPAQUE, f"{env} makes gh run another program", "run gh without it")
        return
    if group in ("--version", "--help", "-h", "") or group in _GH_READ_GROUPS:
        return
    if group.startswith("-"):
        yield blk("other_repo", f"gh {group}: flags before the subcommand cannot be checked",
                  "put flags after the subcommand, e.g. gh pr create --repo owner/name")
        return
    handler = _GH_RULES.get(group)
    if handler is not None:
        yield from handler(t, seg, st, ctx, blk)
    elif group in _GH_GROUPS:
        yield from _gh_write(t, seg, st, ctx, blk)
    else:
        yield blk("other_repo", f"gh {group}: unknown command (a gh alias or extension cannot be checked)",
                  "use a built-in gh command")


def _gh_auth(t, seg, st, ctx, blk):
    verb = t[2] if len(t) > 2 else ""
    if verb != "status":
        yield blk("other_repo", f"gh auth {verb} changes or exposes credentials", "ask the operator to manage gh auth")


def _gh_alias(t, seg, st, ctx, blk):
    verb = t[2] if len(t) > 2 else ""
    if verb not in ("list", "ls", ""):
        yield blk("other_repo", f"gh {t[1]} {verb} can hide commands from the guard",
                  "ask the operator to manage gh aliases and extensions")


def _gh_gist(t, seg, st, ctx, blk):
    verb = t[2] if len(t) > 2 else ""
    if verb not in ("view", "list", "clone", ""):
        yield blk("other_repo", f"gh gist {verb} publishes outside the repo",
                  "put the content in an issue or PR comment on an allowed repo")


def _gh_write(t: List[str], seg: str, st: _State, ctx: GuardContext, blk: Blk) -> Iterator[Block]:
    group, verb = t[1], (t[2] if len(t) > 2 else "")
    what = f"gh {group} {verb}".strip()
    if group in _GH_DESTRUCTIVE and (_GH_DESTRUCTIVE[group] is None or verb in _GH_DESTRUCTIVE[group]):
        yield blk("destructive_git", f"{what} changes settings, secrets or deletes data; that is a human action",
                  "ask the operator to do this by hand")
    if group == "pr" and verb == "merge":
        if "--admin" in t:
            yield blk("force_push", "gh pr merge --admin bypasses branch protection", "leave merging to a person")
        merge = blk("protected_branches", "merging a pull request is a human action",
                    f"leave the PR ready for review; a person merges it, or grants this one merge by sending "
                    f"{merge_grant.COMMAND} owner/name#N")
        merge.grantable = True
        yield merge
    if verb in _GH_READ_VERBS:
        return
    if st.moved:
        yield _moved_write(st, blk, what)
        return
    n_repo = 0
    targets: List[str] = []
    for j in range(2, len(t)):
        a = t[j]
        raw: Optional[str] = None
        if a in ("-R", "--repo"):
            raw = t[j + 1] if j + 1 < len(t) else ""
            n_repo += 1
        elif a.startswith("--repo=") or (a.startswith("-R") and len(a) > 2):
            raw = a[len("--repo="):] if a.startswith("--repo=") else a[2:]
            n_repo += 1
        elif j >= 3:
            raw = parse_origin(a, allow_path=True)
        if raw is None:
            continue
        repo = repo_slug(raw)
        if not repo:
            yield blk("other_repo", f"{what}: cannot read repo '{raw}'", "pass --repo owner/name")
            return
        yield from _check_target(ctx, repo, blk)
        targets.append(repo.lower())
    if n_repo > 1:
        yield blk("other_repo", f"{what}: more than one --repo", "pass --repo once")
    if len(set(targets)) > 1:
        yield blk("other_repo", f"{what}: the URL and --repo name different repos", "name one repo")
    if group == "repo":
        for a in t[3:]:
            if not a.startswith("-") and valid_slug(a):
                yield from _check_target(ctx, a, blk)
                targets.append(a.lower())
        if verb in ("create", "fork") and not targets:
            yield blk("other_repo", f"gh repo {verb} without an allowed owner/name", "name the repo explicitly")
            return
    if not targets:
        yield _unknown_target(blk, f"{what} names no repo, and gh's default repo is not always origin",
                              f"pass --repo {ctx.origin_repo or 'owner/name'}")


def _gh_api(t, seg, st, ctx, blk):
    values: Dict[str, List[str]] = {"method": [], "field": [], "input": []}
    endpoint: Optional[str] = None
    i = 2
    while i < len(t):
        a = t[i]
        i += 1
        flag, val = None, None
        if a.startswith("--"):
            name, eq, v = a.partition("=")
            if name in _API_VALUE_FLAGS:
                flag, val = name, (v if eq else None)
        elif a.startswith("-") and len(a) > 1:
            for k in range(1, len(a)):  # short cluster such as -iXPOST or -ifbody=x
                if "-" + a[k] in _API_VALUE_FLAGS:
                    flag, rest = "-" + a[k], a[k + 1:]
                    val = (rest[1:] if rest.startswith("=") else rest) or None
                    break
        elif endpoint is None:
            endpoint = a
        if flag is None:
            continue
        if val is None:
            val = t[i] if i < len(t) else ""
            i += 1
        kind = _API_VALUE_FLAGS[flag]
        if kind:
            values[kind].append(val)
    fields, inputs = values["field"], values["input"]
    mutation = any("mutation" in a.lower() for a in t[2:])
    ep = (endpoint or "").lstrip("/")
    if ep == "graphql":
        queries = [f.split("=", 1)[1] for f in fields if f.split("=", 1)[0] == "query" and "=" in f]
        if inputs or len(queries) != 1 or any(c in queries[0] for c in "$`") or queries[0].startswith("@"):
            yield blk("other_repo", "gh api graphql without one literal query cannot be checked",
                      "pass the query inline with -f query='...'")
            return
        atfile = any("@" in f for f in fields)
        if (mutation or atfile or inputs) and st.moved:
            yield _moved_write(st, blk, "gh api graphql")
        elif (mutation or atfile or inputs) and BOARD_MARKER not in seg:
            why = "a mutation" if mutation else ("an @file field" if atfile else "--input")
            yield blk("other_repo", f"gh api graphql with {why} cannot be checked",
                      "use gh pr/issue commands with --repo, or the REST API under repos/owner/name")
        return
    method = (values["method"][-1] if values["method"] else ("POST" if fields or inputs else "GET")).upper()
    if method in ("GET", "HEAD"):
        return
    if st.moved:
        yield _moved_write(st, blk, f"gh api {method}")
        return
    if ".." in ep or "//" in ep or "\\" in ep or re.search(r"%(?:2e|2f|5c)", ep, re.I):
        yield blk("other_repo", f"gh api {method} {_short(ep, 60)}: dot segments or encoded separators can reach "
                                "another repo", "spell out repos/owner/name/... plainly")
        return
    m = _REPO_PATH.match(ep)
    repo_root = bool(m) and not ep[m.end():].strip("/")
    if method == "DELETE" or (repo_root and method == "PATCH") or _PROTECTION.search(ep) or _SECRETS.search(ep):
        yield blk("destructive_git", f"gh api {method} {ep}: settings, secrets and deletion are human actions",
                  "ask the operator to do this by hand")
    if m and _PULL_MERGE.search(ep):
        yield blk("protected_branches", "merging a pull request is a human action",
                  "leave the PR ready for review; a person merges it")
    if not m:
        yield _unknown_target(blk, f"gh api {method} {ep or '(no endpoint)'}",
                              "use an endpoint under repos/owner/name for an allowed repo")
        return
    repo = f"{m.group(1)}/{m.group(2)}"
    if "{" in repo or repo.startswith(":") or "/:" in repo:
        yield blk("other_repo", f"gh api {repo}: placeholders are filled from gh's default repo",
                  f"spell out repos/{ctx.origin_repo or 'owner/name'}")
        return
    yield from _check_target(ctx, repo, blk)


# --- programs that run commands from their program text ----------------------
#
# awk, sed, vim, tar and less can run a shell command named in their arguments. Each extractor returns what
# their program text says: the texts it read, the plain literal commands it runs, and a reason when it runs
# something that is not a plain literal (a computed string, the pattern space, a file the guard cannot read).

@dataclass
class _Exec:
    texts: List[str] = field(default_factory=list)  # program text, for the mention check
    runs: List[str] = field(default_factory=list)  # plain literal shell commands it runs
    opaque: Optional[str] = None  # why what it runs cannot be checked
    can_exec: bool = False  # the text can run a program at all


_AWK_STR = re.compile(r'"(?:[^"\\\n]|\\.)*"')
_AWK_REGEX = re.compile(r"((?:^|[(,{;~!&|\n])\s*)/(?:[^/\\\n]|\\.)+/")  # a /regex/ where an operand starts
_AWK_SYSTEM = re.compile(r"\bsystem\s*\(")
_AWK_SYSTEM_LIT = re.compile(r"\bsystem\s*\(\s*\x00(\d+)\x00\s*\)")
_AWK_PIPE = re.compile(r"(?<!\|)\|&?(?!\|)")
_AWK_OTHER_EXEC = re.compile(r"\bclose\s*\(|@load\b")
_AWK_VALUE = {"-v", "--assign", "-F", "--field-separator", "-W"}


def _awk_exec(args: List[str], st: _State) -> _Exec:
    out = _Exec()
    programs: List[str] = []
    files: List[str] = []
    explicit = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-":
            break
        name, eq, val = a.partition("=")
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if name in ("-l", "--load") or (a.startswith("-l") and not a.startswith("--")):
            out.opaque, out.can_exec = "loads an extension", True
            return out
        if name in ("-f", "--file", "-E", "--exec", "-i", "--include") or (
                a[:2] in ("-f", "-E", "-i") and not a.startswith("--") and len(a) > 2):
            files.append(val if eq else (a[2:] if not a.startswith("--") and len(a) > 2 else nxt))
            explicit = explicit or name not in ("-i", "--include")
            i += 1 if (eq or (not a.startswith("--") and len(a) > 2)) else 2
            continue
        if name in ("-e", "--source"):
            programs.append(val if eq else nxt)
            explicit = True
            i += 1 if eq else 2
            continue
        if a == "-W" and nxt.lower().startswith("e"):  # mawk -W exec file
            out.opaque, out.can_exec = "reads its program from a file", True
            return out
        i += 2 if a in _AWK_VALUE else 1
    if not explicit and i < len(args):
        programs.append(args[i])
    for f in files:
        if st.moved and not _path.isabs(f):
            out.opaque, out.can_exec = f"reads its program from {f} in a command that changes directory", True
            return out
        text = _read_script(_norm(f, st.dir), st)
        if text is None:
            out.opaque, out.can_exec = f"reads its program from {f}, which cannot be read", True
            return out
        programs.append(text)
    out.texts = programs
    for prog in programs:
        strings: List[str] = []

        def keep(m: "re.Match[str]") -> str:
            strings.append(m.group(0)[1:-1])
            return f"\x00{len(strings) - 1}\x00"
        masked = _AWK_REGEX.sub(r"\1R", _AWK_STR.sub(keep, prog))
        if _AWK_OTHER_EXEC.search(masked):
            out.can_exec = True
        literal = {m.start() for m in _AWK_SYSTEM_LIT.finditer(masked)}
        for m in _AWK_SYSTEM.finditer(masked):
            out.can_exec = True
            lit = _AWK_SYSTEM_LIT.match(masked, m.start())
            if m.start() not in literal or lit is None:
                out.opaque = out.opaque or "system() with a computed command"
            else:
                out.runs.append(strings[int(lit.group(1))])
        for m in _AWK_PIPE.finditer(masked):
            out.can_exec = True
            after, before = masked[m.end():], masked[:m.start()]
            if re.match(r"\s*getline\b", after):
                lit = re.search(r"\x00(\d+)\x00\s*$", before)
            else:
                lit = re.match(r"\s*\x00(\d+)\x00\s*(?:[;}\n)]|$)", after)
            if lit is None:
                out.opaque = out.opaque or "a | pipe to a computed command"
            else:
                out.runs.append(strings[int(lit.group(1))])
    if any("\\" in r for r in out.runs):
        out.opaque = out.opaque or "a command string with escape sequences"
    return out


def _skip_delimited(s: str, i: int, delim: str) -> int:
    """Index just past the next unescaped delim at or after i."""
    while i < len(s) and s[i] != delim:
        i += 2 if s[i] == "\\" else 1
    return i + 1


def _line_end(s: str, i: int) -> int:
    """End of the line at i, past backslash-newline continuations."""
    while i < len(s) and s[i] != "\n":
        i += 2 if s[i] == "\\" else 1
    return min(i, len(s))


def _sed_script_exec(script: str, out: _Exec) -> None:
    """Find sed's e command (runs the rest of its line, or the pattern space) and the s///e flag."""
    i, n = 0, len(script)
    while i < n:
        c = script[i]
        if c == "\\" and i + 1 < n:  # \cREGEXc address
            i = _skip_delimited(script, i + 2, script[i + 1])
        elif c == "/":
            i = _skip_delimited(script, i + 1, "/")
        elif c in "sy" and i + 1 < n and script[i + 1] not in "\n\\":
            d = script[i + 1]
            i = _skip_delimited(script, _skip_delimited(script, i + 2, d), d)
            flags = re.match(r"[A-Za-z0-9]*", script[i:]).group(0) if c == "s" else ""
            i += len(flags)
            if "e" in flags:
                out.can_exec = True
                out.opaque = out.opaque or "s///e runs the pattern space as a command"
        elif c in "aicrRwWbtTv:#":  # text, a file name or a label to the end of the line
            i = _line_end(script, i + 1)
        elif c == "e":
            end = _line_end(script, i + 1)
            body = script[i + 1:end].strip()
            out.can_exec = True
            if body:
                out.runs.append(body)
            else:
                out.opaque = out.opaque or "e runs the pattern space as a command"
            i = end
        else:
            i += 1


def _sed_exec(args: List[str], st: _State) -> _Exec:
    out = _Exec()
    scripts: List[str] = []
    files: List[str] = []
    explicit = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-":
            break
        nxt = args[i + 1] if i + 1 < len(args) else ""
        name, eq, val = a.partition("=")
        if a == "--sandbox":  # GNU sed refuses e, w and r
            return _Exec()
        if name in ("--expression", "--file"):
            (scripts if name == "--expression" else files).append(val if eq else nxt)
            explicit = True
            i += 1 if eq else 2
            continue
        if a.startswith("--"):
            i += 2 if name == "--line-length" and not eq else 1
            continue
        step = 1
        for k, ch in enumerate(a[1:], 1):
            if ch in "efl":
                value = a[k + 1:] or nxt
                step = 1 if a[k + 1:] else 2
                if ch == "e":
                    scripts.append(value)
                elif ch == "f":
                    files.append(value)
                explicit = explicit or ch in "ef"
                break
            if ch == "i":  # -i[SUFFIX]
                break
        i += step
    if not explicit:  # every operand: -i takes an optional suffix (sed -i '' 's/x/y/' f), so which is the script varies
        scripts += args[i:]
    for f in files:
        text = None if (st.moved and not _path.isabs(f)) else _read_script(_norm(f, st.dir), st)
        if text is None:
            out.opaque, out.can_exec = f"reads its script from {f}, which cannot be read", True
            return out
        scripts.append(text)
    out.texts = scripts
    for s in scripts:
        _sed_script_exec(s, out)
    return out


_VIM_EXEC = re.compile(r"\bsystem(?:list)?\s*\(|\bjob_?start|\btermopen|\bterm_start|\bter(?:minal)?\b|\blibcall"
                       r"|(?:^|[\s:|])(?:py(?:thon)?3?|pyx|lua|perl|ruby|tcl|mz(?:scheme)?|so(?:urce)?|exe(?:cute)?"
                       r"|call|norm(?:al)?|redir)\b")
_VIM_BANG = re.compile(r"^\s*:*\s*[%.,$0-9'<>]*\s*(?:r(?:ead)?\s*|w(?:rite)?\s*)?!(.*)$", re.S)


def _vim_exec(args: List[str], seg: str, st: _State) -> _Exec:
    out = _Exec()
    cmds: List[str] = []
    ex_mode = False
    i = 0
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if a in ("-c", "--cmd") and nxt is not None:
            cmds.append(nxt)
            i += 2
            continue
        if a.startswith("+"):
            cmds.append(a[1:])
        elif a in ("-S", "-u", "-U", "-s", "-i") or a.startswith("--startuptime"):
            if nxt is None or nxt in ("NONE", "NORC"):
                i += 1
                continue
            text = None if (st.moved and not _path.isabs(nxt)) else _read_script(_norm(nxt, st.dir), st)
            if text is None and a != "-i":
                out.opaque, out.can_exec = f"runs commands from {nxt}, which cannot be read", True
                return out
            if a != "-i":
                cmds.append(text or "")
            i += 2
            continue
        elif a.startswith("-") and not a.startswith("--") and set(a[1:]) & set("eE"):
            ex_mode = True
        i += 1
    if ex_mode and ("<" in seg or _mentions_any(st.text)):
        out.opaque, out.can_exec = "reads ex commands from its input", True
    out.texts = cmds
    for text in cmds:
        for line in text.splitlines() or [""]:
            for piece in line.split("|"):
                bang = _VIM_BANG.match(piece)
                if bang:
                    out.can_exec = True
                    out.runs.append(bang.group(1))
                elif _VIM_EXEC.search(piece):
                    out.can_exec = True
                    out.opaque = out.opaque or f"'{_short(piece.strip(), 40)}' can run a program"
    return out


_TAR_EXEC = ("--to-command", "--use-compress-program", "--info-script", "--new-volume-script", "--rsh-command",
             "--rmt-command", "--checkpoint-action")
_TAR_SHORT_VALUE = set("bCfgHKLNTVX")


def _tar_exec(args: List[str], st: _State) -> _Exec:
    out = _Exec(texts=list(args))
    if "TAR_OPTIONS" in st.text:
        out.opaque, out.can_exec = "TAR_OPTIONS can add options that run a program", True
        return out
    if args and not args[0].startswith("-") and set(args[0]) & set("IF"):
        out.opaque, out.can_exec = f"old-style options {args[0]} can run a program", True
        return out
    for k, a in enumerate(args):
        nxt = args[k + 1] if k + 1 < len(args) else ""
        if a == "--":
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            opt = next((o for o in _TAR_EXEC if len(name) > 2 and o.startswith(name)), None)
            if opt is None:
                continue
            value = val if eq else nxt
            out.can_exec = True
            if opt == "--checkpoint-action":
                if value.startswith("exec="):
                    out.runs.append(value[len("exec="):])
            else:
                out.runs.append(value)
        elif a.startswith("-") and len(a) > 1:
            for j, ch in enumerate(a[1:], 1):
                if ch in "IF":
                    out.can_exec = True
                    out.runs.append(a[j + 1:] or nxt)
                    break
                if ch in _TAR_SHORT_VALUE:
                    break
    return out


_LESS_ENV = re.compile(r"(?<![A-Za-z0-9_])(?:LESS[A-Z_]*|VISUAL|EDITOR)(?![A-Za-z0-9_])")


def _less_exec(args: List[str], st: _State) -> _Exec:
    out = _Exec()
    m = _LESS_ENV.search(st.text)
    if m:
        out.opaque, out.can_exec = f"{m.group(0)} can make less run a program", True
        return out
    for a in args:
        if not a.startswith("+"):
            continue
        cmd = a.lstrip("+")
        out.texts.append(cmd)
        if "|" in cmd:
            out.can_exec = True
            out.opaque = out.opaque or "a | command pipes to a program"
        elif "!" in cmd:
            out.can_exec = True
            out.runs.append(cmd.split("!", 1)[1])
    return out


_RUNNERS: Dict[str, Callable[[List[str], str, _State], _Exec]] = {
    **{p: (lambda a, s, st: _awk_exec(a, st)) for p in ("awk", "gawk", "mawk", "nawk")},
    **{p: (lambda a, s, st: _sed_exec(a, st)) for p in ("sed", "gsed")},
    **{p: _vim_exec for p in ("vim", "vi", "nvim", "ex", "view", "vimdiff", "gvim", "rvim", "evim")},
    **{p: (lambda a, s, st: _tar_exec(a, st)) for p in ("tar", "gtar", "bsdtar")},
    "less": lambda a, s, st: _less_exec(a, st),
}


def _cmd_runner(t, seg, st, ctx, blk, gitenv):
    """awk, sed, vim, tar and less: block when their program text can run a program and mentions gh/git/github, or
    runs something that is not a plain literal; judge each plain literal command as a shell command."""
    prog = _prog(t[0])
    ex = _RUNNERS[prog](t[1:], seg, st)
    hint = "run the command directly, with every word spelled out"
    if ex.opaque:
        yield blk(OPAQUE, f"{prog} {ex.opaque}, so what it runs cannot be checked", hint)
        return
    if ex.can_exec and any(_mentions_any(x) for x in ex.texts + ex.runs):
        yield blk(OPAQUE, f"{prog} program text that can run a program mentions gh/git or GitHub, which cannot be "
                          "checked", "run gh/git directly")
        return
    for body in ex.runs:
        yield from _judge_body(body, st, ctx, blk, prog)


def _cmd_rg(t, seg, st, ctx, blk, gitenv):
    """rg only runs a program through --pre (with --pre-glob) or options from RIPGREP_CONFIG_PATH."""
    if "RIPGREP_CONFIG_PATH" in st.text:
        yield blk(OPAQUE, "RIPGREP_CONFIG_PATH can add rg --pre, which runs a program", "run rg without it")
        return
    for a in t[1:]:
        if a == "--":
            break
        if a.split("=", 1)[0] in ("--pre", "--pre-glob"):
            yield blk(OPAQUE, f"rg {a.split('=', 1)[0]} runs a program on every file", "run rg without --pre")
            return


_GH_RULES = {"api": _gh_api, "auth": _gh_auth, "alias": _gh_alias, "extension": _gh_alias, "ext": _gh_alias,
             "extensions": _gh_alias, "gist": _gh_gist}

_COMMANDS: Dict[str, Callable[..., Iterator[Block]]] = {
    "export": _cmd_export, "declare": _cmd_export, "typeset": _cmd_export, "set": _cmd_export,
    "rm": _cmd_rm, "git": _cmd_git, "gh": _cmd_gh, "find": _cmd_find, "eval": _cmd_eval,
    "xargs": _wrapped(_XARGS_VALUE), "parallel": _wrapped(_PARALLEL_VALUE, (":::", "::::")),
    "sudo": _wrapped(_SUDO_VALUE), "doas": _wrapped(_SUDO_VALUE),
    **{s: _cmd_shell for s in (*_SHELLS, "source", ".")},
    **{p: _cmd_runner for p in _RUNNERS}, "rg": _cmd_rg,
}

# --- log and self-test -----------------------------------------------------


def log_block(ctx: GuardContext, block: Block) -> None:
    """Append the block to the guard log in the default run-log root (none outside git)."""
    root = log_root(ctx.common_dir)
    if root is not None:
        runlog.guard_log_append(runlog.guard_log_path(root), {
            "tool": block.tool, "rule": block.rule, "reason": block.reason, "target": block.target})


# Variables a Windows child process needs to start at all: Python before 3.11 cannot seed its hash randomisation
# without SYSTEMROOT and dies with exit 1. Copied from this process when the given environment lacks them.
_OS_ENV = ("SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")


def selftest_run(hook_cmd: List[str], ctx: GuardContext,
                 env: Optional[Mapping[str, str]] = None) -> Tuple[bool, str]:
    """Pipe a canned always-blocked event (a write to the allow list) through the hook.

    Returns (blocked, stderr): blocked is True iff the hook exits 2; stderr is the hook's (or the launch error's)
    text, for diagnosing a failure."""
    event = {"hook_event_name": "PreToolUse", "tool_name": "Write", "cwd": ctx.cwd,
             "tool_input": {"file_path": str(ctx.allowlist_path), "content": "{}"}}
    run_env = dict(os.environ if env is None else env)
    for key in _OS_ENV:
        if key not in run_env and key in os.environ:
            run_env[key] = os.environ[key]
    run_env["NOT_ME_HOME"] = str(Path(ctx.allowlist_path).parent)
    try:
        proc = subprocess.run(hook_cmd, input=json.dumps(event), capture_output=True, text=True, timeout=30,
                              cwd=ctx.cwd, env=run_env)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{exc.__class__.__name__}: {exc}"
    if proc.returncode == 2:
        return True, proc.stderr or ""
    return False, f"exit {proc.returncode}: {proc.stderr or ''}"


def selftest(hook_cmd: List[str], ctx: GuardContext, env: Optional[Mapping[str, str]] = None) -> bool:
    """True iff the hook blocks the canned always-blocked event (see selftest_run)."""
    return selftest_run(hook_cmd, ctx, env)[0]
