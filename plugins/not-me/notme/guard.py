"""PreToolUse guard: decide whether a tool call may run.

`decide(event, ctx)` returns None (allow) or a Block. It reads git state in the
working directory (push URLs, current branch, aliases) lazily through
`ctx.git`, and loads not-me config only when a rule needs it. Building the
context (`GuardContext.from_project`) and the inactive path start no
processes: the checkout is found by walking up for `.git`.

Rule groups, each switched by `guard.<rule>` in project.md:
  other_repo          writes may only target allowed repos
  protected_branches  no commits, pushes, merges or resets on protected branches
  force_push          no history rewrites or remote branch deletion
  destructive_git     shown as "destructive": hard resets, forced branch deletes,
                      clean -x, rm -rf .git, and GitHub settings, secrets and deletions
Self-protection (the allow list, the plugin's own files, and not-me config
during a run) is always on.

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
cannot see through ($(...) around gh/git, bash -c, GIT_DIR=..., a command name
in a variable, inline interpreter code, scripts that mention gh/git) is
blocked, so it fails closed.
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

from . import paths, runlog, writes
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
MAX_COMMAND = 32 * 1024  # longer Bash commands are blocked unread: checking them could outrun the hook timeout
MAX_NESTING = 64  # deeper $( ), backtick or ${ } nesting is blocked unread
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
# words that make a forged terminal dangerous: driving the not-me CLI or its human checks through a pty
_PTY_TRIGGER = re.compile(r"notme|allow-repo|(?<![A-Za-z0-9_-])pause(?![A-Za-z0-9_-])|operator-token|NOT_ME_")
# words a script file run through a shell may not contain (they drive the not-me CLI where the guard cannot see)
_SCRIPT_SELF = re.compile(r"notme|allow-repo|operator-token|NOT_ME_")
_PTY_CODE = re.compile(r"(?<![A-Za-z])pty(?![A-Za-z])|openpty|forkpty|spawn\s*\(")
# code that forges a terminal or leaves the session's process tree: blocked in inline code and scripts outright
_PRIMITIVES = re.compile(r"(?<![A-Za-z_])pty(?![A-Za-z_])|openpty|forkpty|os\.fork\b|(?<![A-Za-z_.])fork\s*\(|setsid"
                         r"|(?<![A-Za-z_])daemon(?!\s*=)|start_new_session\s*=\s*True|detached\s*:\s*true")
GLOB_LIMIT = 5000  # paths a token-read glob may visit before the guard assumes it reaches the token
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
    r"|(?<![A-Za-z0-9_-])(?:script|expect|unbuffer|socat|ssh|tmux|screen|pause)(?![A-Za-z0-9_-])"
    # any file reader, copier or < redirection: its arguments may expand into the not-me home
    r"|(?<![<\d&])<(?![<(&])|(?<![A-Za-z0-9_-])(?:" + "|".join(sorted(_TOKEN_READERS)) + r")(?![A-Za-z0-9_-])"
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
_DIR_NAMES = {"source", ".", "eval", "exec", "alias", "function", "trap"}  # as the command name
_DIR_VARS = "PWD|OLDPWD|CDPATH|HOME|DIRSTACK|cdpath|dirstack"
_DIR_VAR_SET = re.compile(r"(?<![A-Za-z0-9_$])(?:" + _DIR_VARS + r")(?:\[[^\[\]]*\])?\s*(?:[-+*/%&|^]|<<|>>)?=(?!=)")
_DIR_VAR_WORD = re.compile(r"(?:-[A-Za-z]*)?(?:" + _DIR_VARS + r")")
_DIR_VAR_DEFAULT = re.compile(r"\$\{(?:" + _DIR_VARS + r")(?:\[[^\[\]]*\])?:?=")
_DECLARE = {"declare", "typeset", "local", "export", "readonly", "integer", "float", "private"}
_NAME_ARGS = {"read", "mapfile", "readarray", "unset", "let", "getopts"}
_CD_HINT = ("use git -C <path> or gh -R owner/name instead of cd; if a cd is needed, run it in a separate Bash "
            "call first")
_HTTP_CLIENTS = {"curl", "wget", "http", "https", "xh"}
_GIT_READ = {"", "status", "log", "diff", "show", "fetch", "pull", "add", "restore", "stash", "tag", "rev-parse",
             "ls-files", "worktree", "init", "clone", "cherry-pick", "revert", "mv", "rm", "grep", "blame",
             "describe", "shortlog", "ls-remote"}
# git subcommands that change nothing; any other one counts as a write next to a directory change or GIT_*
_GIT_READ_ONLY = {"", "status", "log", "diff", "show", "rev-parse", "ls-files", "ls-tree", "ls-remote", "cat-file",
                  "grep", "blame", "annotate", "describe", "shortlog", "rev-list", "merge-base", "show-ref",
                  "for-each-ref", "show-branch", "check-ignore", "name-rev", "cherry", "range-diff", "diff-tree",
                  "diff-files", "diff-index", "whatchanged", "count-objects", "var", "help", "version"}
# subcommands that only list with these arguments: (words, True = every argument, False = the first one)
_GIT_LISTING: Dict[str, Tuple[Set[str], bool]] = {
    "branch": ({"-a", "-r", "-v", "-vv", "-l", "--list", "--all", "--remotes", "--verbose", "--show-current"}, True),
    "tag": ({"-l", "--list", "-n"}, True),
    "remote": ({"", "-v", "--verbose", "show", "get-url"}, False),
    "stash": ({"list", "show"}, False),
    "worktree": ({"list"}, False),
    "config": ({"--get", "--get-all", "--get-regexp", "--list", "-l"}, False),
}
_GIT_CONFIG_KEYS =("remote.", "url.", "branch.", "alias.", "include.", "includeif.", "credential.")
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
        self.sub_spans: List[Tuple[int, int]] = []
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
                self.param()
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
                out.append(self.param())
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
        self.sub_spans.append((body, self.i - 1))
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

    def param(self) -> str:
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
            elif c == "'":
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
                self.param()
            else:
                self.i += 1
        self.nest(-1)
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
    return Parsed(lx.commands, [lx.without_bodies(a, b) for a, b in lx.sub_spans],
                  lx.without_bodies(0, len(cmd)), lx.funcdef)


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
        if write_ctx is not None:
            self.__dict__["write_ctx"] = write_ctx  # shadows the cached property

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
        cands = _file(tool, str(tin.get("file_path") or tin.get("notebook_path") or ""), cwd, ctx)
    elif tool in _READ_TOOLS:
        cands = _read_tool(tool, tin, cwd, ctx)
    elif _MCP_TOOL.match(tool):
        cands = _mcp(tool, tin, ctx)
    else:
        return None
    for block in cands:
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


def _read_tool(tool: str, tin: Mapping[str, Any], cwd: str, ctx: GuardContext) -> Iterator[Block]:
    """Read, NotebookRead, LS, Grep and Glob may not reach the operator token (or list the not-me home)."""
    if tool in ("Read", "NotebookRead"):
        yield from _token_file(tool, str(tin.get("file_path") or tin.get("notebook_path") or ""), cwd, ctx)
        return
    raw = str(tin.get("path") or "")
    here = _norm(cwd, os.getcwd())
    base = _norm(_expand_home(raw, ctx), here) if raw else here
    home = _norm(str(ctx.home), cwd)
    token = _norm(_path.join(str(ctx.home), OPERATOR_TOKEN), cwd)
    pattern = str(tin.get("pattern" if tool == "Glob" else "glob") or "")
    blk = _blk(_short(f"{tool} {raw} {pattern}".strip()))
    hit = base in (home, token) or OPERATOR_TOKEN in raw.lower() or OPERATOR_TOKEN in pattern.lower()
    if not hit and tool != "LS" and _under(token, base):
        rel = token[len(base):].lstrip(_path.sep).replace(_path.sep, "/")
        # a missing or matching filter reaches the token (fnmatch's * also crosses "/": an over-approximation)
        hit = not pattern or any(fnmatch.fnmatch(x, pattern) for x in (rel, OPERATOR_TOKEN))
    if not hit and tool == "Glob" and pattern:
        full = _norm(_expand_home(pattern, ctx), base)
        hit = full in (home, token) or _under(full, home)
    if hit:
        yield blk(SELF, "the operator token is the operator's own confirmation", "ask the operator to confirm by hand")


def _token_file(tool: str, fpath: str, cwd: str, ctx: GuardContext) -> Iterator[Block]:
    """The operator-token file is the operator's own: the agent may not read or change it."""
    if fpath and (_base(fpath).lower() == OPERATOR_TOKEN
                  or _norm(fpath, cwd) == _norm(_path.join(str(ctx.home), OPERATOR_TOKEN), cwd)):
        yield _blk(_short(f"{tool} {fpath}"))(SELF, "the operator token is the operator's own confirmation",
                                              "ask the operator to confirm by hand")


def _file(tool: str, fpath: str, cwd: str, ctx: GuardContext) -> Iterator[Block]:
    if not fpath:
        return
    yield from _token_file(tool, fpath, cwd, ctx)
    blk = _blk(_short(f"{tool} {fpath}"))
    full = _norm(fpath, cwd)
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
    checkout moved to, and memoised lookups. Text scans see both the source and its line continuations joined."""

    def __init__(self, cwd: str, parsed: Parsed, raw: str):
        self.dir = cwd
        self.text = parsed.code + "\n" + parsed.code.replace("\\\n", "")
        self.raw = raw + "\n" + raw.replace("\\\n", "")  # with here-document bodies, for the pty checks
        self.background = bool(_BACKGROUND.search(self.text))
        self.moved = _moves(parsed, raw, cwd)  # what changes the directory, or None
        self.switched: Dict[str, str] = {}
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
    m = _DIR_VAR_DEFAULT.search(raw)
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
    plain = _unshell(cmd)
    if "allow-repo" in plain and any(m.group(1) or "--everywhere".startswith(m.group(2))
                                     for m in _ALLOW_EDIT.finditer(plain)):
        yield blk(SELF, "only the operator changes the not-me allow list", "ask the operator to run /not-me:allow-repo")
    if OPERATOR_TOKEN in plain.lower():
        yield blk(SELF, "the operator token is the operator's own confirmation", "ask the operator to confirm by hand")
    if ALLOWLIST_NAME in cmd.lower() or str(ctx.allowlist_path).lower() in cmd.lower():
        yield blk(SELF, "the not-me allow list is off limits to the agent",
                  "ask the operator to run /not-me:allow-repo, or read it with /not-me:config")
    try:
        parsed = lex(cmd)
    except Unparsed as exc:  # not even self-protection can be checked: blocked whatever rules are on
        yield blk(SELF, f"the guard cannot read this command with confidence: {exc}",
                  "split it into shorter, simpler commands with plain quoting")
        return
    for c in parsed.commands:
        fed = any(_prog(w) in _FED_PROGS or _INTERP.match(_prog(w)) for w in c.words)
        if fed and any(_mentions_github(b) for b in c.bodies):
            yield blk(OPAQUE, "a heredoc fed to a shell or interpreter runs gh/git that cannot be checked",
                      "run gh/git directly")
    if any(has_gh_git(_unshell(s)) for s in parsed.subs):
        yield blk(OPAQUE, "gh/git inside $(...), backticks or <(...) cannot be checked",
                  "run gh/git directly, not inside $(...)")
    st = _State(cwd, parsed, cmd)
    for c in parsed.commands:
        yield from _command(list(c.words), c.raw, st, ctx)


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


def _reaches_token(pattern: str, base: str, token: str, recursive: bool) -> bool:
    """True when pattern (a path or glob, relative to base) names the token, or a directory holding it
    when recursive. Globs are expanded for real; one that visits more than GLOB_LIMIT paths counts as
    reaching the token when its fixed leading directory holds it."""
    full = _path.join(base, _path.expanduser(pattern)) if not _path.isabs(_path.expanduser(pattern)) \
        else _path.expanduser(pattern)

    def hit(path: str) -> bool:
        n = _norm(path, base)
        return n == token or (recursive and _under(token, n))
    if not _GLOB.search(pattern):
        return hit(full)
    try:
        for k, match in enumerate(glob.iglob(full, recursive=True)):
            if hit(match):
                return True
            if k >= GLOB_LIMIT:
                fixed = _PATH_SEP.split(full)
                fixed = fixed[:next(i for i, part in enumerate(fixed) if _GLOB.search(part))]
                return _under(token, _norm(_path.sep.join(fixed) or _path.sep, base))
    except (OSError, ValueError, re.error):
        return True  # cannot expand: fail closed
    return False


def _token_reads(t: List[str], seg: str, st: _State, ctx: GuardContext, blk: Blk) -> Iterator[Block]:
    """Reading or copying commands (and < / $(< operands) whose arguments expand to the operator token."""
    prog = _prog(t[0]) if t else ""
    targets = [m.group(1) for m in _IN_REDIRECT.finditer(seg)]
    if prog in _TOKEN_READERS:
        targets += [a[3:] if prog == "dd" and a.startswith("if=") else a for a in t[1:] if not a.startswith("-")]
    if not targets:
        return
    token = st.once(("token",), lambda: _norm(_path.join(str(ctx.home), OPERATOR_TOKEN), st.dir))
    recursive = prog in _TOKEN_COPIERS or (prog in ("grep", "egrep", "fgrep", "rg") and (
        prog == "rg" or "r" in _short_flags(t[1:]).lower() or "--recursive" in t[1:]))
    for a in targets:
        if (_may_reach_token(a, token, recursive) if st.moved and not _path.isabs(a)
                else _reaches_token(_expand_home(a, ctx), st.dir, token, recursive)):
            yield blk(SELF, "the operator token is the operator's own confirmation",
                      "ask the operator to confirm by hand")
            return


def _may_reach_token(arg: str, token: str, recursive: bool) -> bool:
    """After a directory change the base of a relative path is unknown: could arg name the token, or (recursive)
    a directory that holds it? Judged by its last part against the names on the token's path."""
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
    """The arguments of a not-me CLI call (bin/notme, cli/notme.py, a shell or python running it); else None."""
    prog = _prog(t[0])
    if prog in _NOTME_PROGS:
        return t[1:]
    if prog in _SHELLS or prog == "notme-python" or _INTERP.match(prog):
        for k, a in enumerate(t[1:], 1):
            if a.startswith("-"):
                if a in ("-c", "-m", "-e", "--eval"):
                    return None  # inline code: the interpreter checks handle it
                continue
            return _notme_args(t[k:]) if _prog(a) in _NOTME_PROGS | {"notme-python"} else None
    return None


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


def _cmd_shell(t, seg, st, ctx, blk, gitenv):
    head = _head(t[0])
    args = t[1:]
    if head in _SHELLS and any(a.startswith("-") and not a.startswith("--") and "c" in a[1:] for a in args):
        if has_gh_git(seg):
            yield blk(OPAQUE, f"{head} -c wrapping gh/git cannot be checked", "run gh/git directly")
        return
    i = 0
    while i < len(args) and args[i].startswith(("-", "+")):
        i += 2 if args[i] in ("-o", "+o", "-O", "+O") else 1
    if i < len(args):
        yield from _script(args[i], st, blk)


def _cmd_eval(t, seg, st, ctx, blk, gitenv):
    if has_gh_git(seg):
        yield blk(OPAQUE, "eval wrapping gh/git cannot be checked", "run gh/git directly")


def _wrapped(takes_value: Set[str], stop: Tuple[str, ...] = ()):
    """Handler for xargs, parallel and sudo: check the command they run."""
    def handler(t, seg, st, ctx, blk, gitenv):
        if st.moved and has_gh_git(_unshell(seg)):
            yield _moved_write(st, blk, f"gh/git through {_prog(t[0])}")
            return
        rest = _skip_options(t[1:], takes_value)
        for k, a in enumerate(rest):
            if a in stop:
                rest = rest[:k]
                break
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
        else:
            if a.split("=")[0] in ("--git-dir", "--work-tree", "--config-env", "--namespace", "--exec-path",
                                   "--super-prefix"):
                yield blk(OPAQUE, f"git {a.split('=')[0]} cannot be checked", "use git -C <checkout> instead")
            i += 1
    sub = t[i] if i < len(t) else ""
    known = sub in _GIT_READ or sub in _GIT_RULES
    if not known or _git_writes(sub, t[i + 1:]):
        if st.moved:
            yield _moved_write(st, blk, f"git {sub}".strip())
            return
        if "GIT_" in st.plain_text:
            yield blk(OPAQUE, "GIT_* variables in a command with a git write can change where git reads its repo "
                              "or config", "run git without GIT_* variables")
    if any(v.lower().startswith("alias.") for v in c_values) or (c_values and not known):
        yield blk(OPAQUE, "git -c defining or calling an alias cannot be checked", "run the git command directly")
    if not known:
        alias = _git_config(ctx, st, d, f"alias.{sub}")
        if alias and ("push" in alias or alias.startswith("!") or "send-pack" in alias):
            yield blk("other_repo", f"git alias '{sub}' runs '{alias}'", "run the underlying git command directly")
    handler = _GIT_RULES.get(sub)
    if handler is not None:
        yield from handler(_GitCall(ctx, st, blk, d, sub, t[i + 1:], bool(c_values)))


def _git_writes(sub: str, args: List[str]) -> bool:
    """True unless this git subcommand only reads (a listing form such as `git branch -a` counts as a read)."""
    if sub in _GIT_READ_ONLY:
        return False
    if sub not in _GIT_LISTING:
        return True
    words, every = _GIT_LISTING[sub]
    if every:
        return not all(a in words for a in args)
    return (args[0] if args else "") not in words


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
    for a in c.args:
        k = a.lower()
        if k.startswith(_GIT_CONFIG_KEYS) or k == "core.sshcommand":
            yield c.blk("other_repo", f"git config {a} can redirect pushes", "ask the operator to change git config")


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


def _git_push(c: _GitCall) -> Iterator[Block]:
    ctx, st, blk, d = c.ctx, c.st, c.blk, c.d
    force = delete = mirror = all_ = False
    positional: List[str] = []
    repo_opt: Optional[str] = None
    for a in c.args:
        if a.startswith("--repo="):
            repo_opt = a[len("--repo="):]
        elif a in ("-o", "--push-option", "--receive-pack", "--exec", "--repo") or \
                a.startswith(("--receive-pack=", "--exec=")):
            yield blk("other_repo", f"git push {a.split('=')[0]}: option not supported by the guard",
                      "push without that option")
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
        yield blk("protected_branches", "merging a pull request is a human action",
                  "leave the PR ready for review; a person merges it")
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


_GH_RULES = {"api": _gh_api, "auth": _gh_auth, "alias": _gh_alias, "extension": _gh_alias, "ext": _gh_alias,
             "extensions": _gh_alias, "gist": _gh_gist}

_COMMANDS: Dict[str, Callable[..., Iterator[Block]]] = {
    "export": _cmd_export, "declare": _cmd_export, "typeset": _cmd_export, "set": _cmd_export,
    "rm": _cmd_rm, "git": _cmd_git, "gh": _cmd_gh, "find": _cmd_find, "eval": _cmd_eval,
    "xargs": _wrapped(_XARGS_VALUE), "parallel": _wrapped(_PARALLEL_VALUE, (":::", "::::")),
    "sudo": _wrapped(_SUDO_VALUE), "doas": _wrapped(_SUDO_VALUE),
    **{s: _cmd_shell for s in (*_SHELLS, "source", ".")},
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
