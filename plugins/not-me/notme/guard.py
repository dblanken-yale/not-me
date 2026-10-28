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
  other_repo          git and gh writes may only target allowed repos, and
                      nothing may change where git pushes
  protected_branches  no commits, merges, rebases, cherry-picks, am or hard
                      resets on a protected branch, and no pushes to one
  force_push          no history rewrites or remote branch deletion
  destructive_git     shown as "destructive": GitHub settings, secrets,
                      rulesets, releases and deletions, rm -rf .git, clean -x,
                      forced deletes of protected branches
Self-protection is always on, in every directory: the operator token, the
live-mode epoch and marker and the allow list (by
file path), `notme allow-repo add|remove`, `notme pause off` and `notme mode`
other than show or dry-run (as parsed commands), a pseudo-terminal or
detached process around notme, the plugin's own files, and not-me config
during a run. Live mode is signed: `mode: live` counts only next to a marker
that `notme mode live` writes with the operator token, so no file edit can
turn it on; the Edit/Write check on the global project.md is an early,
friendly refusal, not the boundary. Merging pull requests is not the guard's
business: no not-me skill merges, and a person may merge or ask Claude to.

Bash commands are split by `lex`, one linear pass that reads quotes,
backslashes, line continuations, substitutions, here-documents and
redirections the way the shell does. Every simple command it finds is judged,
including the ones inside $( ) and backticks. Text the guard does not parse
(a command it cannot lex, a shell -c body nested too deep, inline interpreter
code, a script file, a command name it cannot resolve, words xargs adds) is
opaque: it is allowed unless its decoded text holds one of the words in
_DANGER or _SELF_TEXT, and then blocked under the first relevant rule that is
on. The guard does not follow cd: writes are judged in the hook's working
directory, or the `git -C` / `gh -R` they name.

Residual risk: a token file readable by the same OS user cannot be fully hidden
from an agent running as that user; this guard is the enforcement, and the
operator token is a second layer, not a secret.
"""
from __future__ import annotations

import bisect
import fnmatch
import glob
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Mapping, Optional, Set, Tuple

from . import paths, redact, runlog, writes
from .frontmatter import parse as parse_frontmatter  # stdlib only: no config stack
from .writes import parse_origin, valid_slug

RULES = writes.GUARD_RULES
RULE_LABEL = {"destructive_git": "destructive"}
SELF = "self_protect"
OPAQUE = "opaque"  # cannot be parsed: reported under the first enabled rule in Block.rules

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
MAX_COMMAND = 32 * 1024  # longer Bash commands are not lexed: their text is judged as opaque
MAX_NESTING = 64  # deeper $( ), backtick or ${ } nesting is not lexed: its text is judged as opaque
MAX_BODY_DEPTH = 4  # shell bodies (bash -c, eval) nested deeper than this are judged as opaque text
PREFILTER_MAX = 2048  # longer commands skip the regex prefilter and always get the full check
GLOB_LIMIT = 5000  # paths a glob may expand to before the guard stops expanding it

GitFn = Callable[[List[str], str], Optional[str]]

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
ALLOW_WHY, ALLOW_HINT = "only the operator changes the not-me allow list", "ask the operator to run /not-me:allow-repo"
HOME_WHY = "the not-me home holds the operator's own secrets"

# --- patterns --------------------------------------------------------------

_HOST_SLUG = re.compile(r"^github\.com/([^/]+/[^/]+)$")
_GH_GIT = re.compile(r"(^|[^A-Za-z0-9_.-])(gh|git)([^A-Za-z0-9_.-]|$)")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\[\]]*\])?\+?=")
_PATH_SEP = re.compile(r"[/\\]")
_SCP_REMOTE = re.compile(r"^[^/]*@[^/]*:")
_PARAM_DEFAULT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:?[-=+?]([^{}]*)\}")
_ESC_SEQ = re.compile(r"\\(?:x([0-9A-Fa-f]{1,2})|([0-7]{1,3})|u([0-9A-Fa-f]{4}))")  # \x67, \147, g
_SHELL_PIECES = re.compile(r"\$\{[^{}]*\}|\$\([^()]*\)|`[^`]*`|\$(?=['\"])")
_GLOB = re.compile(r"[*?\[]")
_BACKGROUND = re.compile(r"(?<![&<>|])&(?![&>])")  # a lone & (also zsh &! and ( ... ) &), not &&, 2>&1 or &>
_LOCALHOST = re.compile(r"^(?:[^@]+@)?(?:localhost|127(?:\.\d+){3}|::1|\[::1\]|0\.0\.0\.0)$", re.IGNORECASE)
_NOTME_WORD = re.compile(r"notme", re.IGNORECASE)
# interpreter program names (lowercased, versioned or not); the prefilter is built from the same pattern
_INTERP_NAMES = (r"(?:python|pythonw|py|node|nodejs|perl|ruby|php|deno|bun|lua|luajit|rscript|osascript|pwsh"
                 r"|powershell|julia|tclsh|wish)[0-9.]*")
_INTERP = re.compile(r"^" + _INTERP_NAMES + r"$")
_REPO_PATH = re.compile(r"^repos/([^/]+)/([^/?]+)(/|\?|$)")
_PROTECTION = re.compile(r"/branches/[^/]+/protection(/|$)")
_SECRETS = re.compile(r"/(actions|dependabot|codespaces)/(secrets|variables)(/|$)"
                      r"|/environments/[^/]+/(secrets|variables)(/|$)")
_RULESETS = re.compile(r"/rulesets(/|$)")
_MCP_TOOL = re.compile(r"^mcp__.*github.*", re.IGNORECASE)
_READ_MCP_PREFIX = re.compile(r"^(get|list|search)_[a-z0-9_]+$")
_MCP_WRITE_WORDS = re.compile(
    r"(^|_)(merge|create|update|delete|add|remove|set|write|push|run|rerun|dismiss|request|submit|assign|close|"
    r"reopen|fork|edit|lock|unlock|cancel|enable|disable|trigger|mark|star|approve|comment|upload|transfer)(_|$)")

# Opaque text: what decoded text the guard does not parse may not hold, with the rules a match is reported under
# (the first one that is on). Matched against every spelling _spellings makes. Every matcher is linear in the text:
# a two-part one finds its first part once per shell segment and looks for the second part after it.
_PB, _FP, _OR, _DG = "protected_branches", "force_push", "other_repo", "destructive_git"
_SEGMENT = re.compile(r"[^;&|\n]+")
Matcher = Callable[[str], Optional[str]]


def _found(pattern: "re.Pattern[str]") -> Matcher:
    return lambda v: (lambda m: m.group(0) if m else None)(pattern.search(v))


def _then(first: "re.Pattern[str]", second: "re.Pattern[str]") -> Matcher:
    """first, then second later in the same shell segment (between ;, &, | and newlines)."""
    def match(v: str) -> Optional[str]:
        for seg in _SEGMENT.finditer(v):
            m = first.search(v, seg.start(), seg.end())
            n = second.search(v, m.end(), seg.end()) if m else None
            if n:
                return v[m.start():n.end()]
        return None
    return match


_DANGER: Tuple[Tuple[Matcher, Tuple[str, ...]], ...] = (
    (_found(re.compile(r"(?<![^\s'\"(\[,;|&{=`])push(?=[\s'\")\],;|&}`]|$)")), (_PB, _FP, _OR)),  # push as a word
    (_found(re.compile(r"reset\s+(?:\S+\s+)?--hard")), (_PB,)),
    # gh pr merge|close --delete-branch (or -d) deletes the remote branch
    (_then(re.compile(r"(?<![A-Za-z0-9_-])pr\s+(?:merge|close)(?![A-Za-z0-9_-])"),
           re.compile(r"--delete-branch|(?<![A-Za-z0-9_-])-[A-Za-z]*d(?![A-Za-z0-9_-])")), (_FP,)),
    (_found(re.compile(r"-X\s*=?\s*DELETE|--method[\s=]+DELETE", re.IGNORECASE)), (_DG,)),
    # GitHub secrets: gh secret set|delete|remove and the secrets API paths
    (_found(re.compile(r"(?<![A-Za-z0-9_-])secret\s+(?:set|delete|remove)(?![A-Za-z0-9_-])"
                r"|/(?:actions|dependabot|codespaces)/secrets(?![A-Za-z0-9_-])|/environments/[^/\s]+/secrets"
                r"(?![A-Za-z0-9_-])", re.IGNORECASE)), (_DG,)),
    (_found(re.compile(r"branch\s+(?:\S+\s+)?-[A-Za-z]*D")), (_DG,)),
    (_found(re.compile(r"clean\s+(?:\S+\s+)?-[A-Za-z]*[xX]")), (_DG,)),
    (_then(re.compile(r"rm\s+-[A-Za-z]*[rR]"), re.compile(r"\.git(?![A-Za-z0-9_-])")), (_DG,)),
)
# protected path names: self-protection in any opaque text
_SELF_TEXT = re.compile(r"operator-token|mode-live\.(?:sig|epoch)|allowed-repos\.json", re.IGNORECASE)
# the not-me CLI's human switches, in text the guard does not parse that sits in the command itself (not in a
# script file, whose own text may describe them; the CLI's restricted mode is the second layer there)
_NOTME_TEXT = re.compile(r"allow-repo[\s,]+(?!list(?![A-Za-z0-9_-]))[^\s,]"
                         r"|(?<![A-Za-z0-9_-])pause[\s,]+-*off(?![A-Za-z0-9_-])|(?<![A-Za-z0-9_-])mode[\s,]+live",
                         re.IGNORECASE)
_OPAQUE_HINT = "run the git, gh or notme command directly, with every word spelled out, so the guard can check it"

# Bash commands that cannot matter to any rule skip the full check (applied with quotes/backslashes removed).
_PREFILTER = re.compile(
    r"(?<![A-Za-z0-9_-])(gh|git|rm|find|xargs|parallel|sudo|doas|notme|bash|sh|zsh|dash|ksh|fish|source|exec|eval"
    r"|script|expect|unbuffer|socat|ssh|tmux|screen|nohup|setsid|disown|daemon|at|batch|systemd-run"
    r"|start-stop-daemon|launchctl|crontab|(?i:" + _INTERP_NAMES + r"))(?![A-Za-z0-9_-])"
    r"|not-me|NOT_ME_|operator-token|mode-live|allowed-repos"
    r"|[$`/~*?\[\\&]|(?:^|[\s;|&(])\.(?:\s|$)|\.(?:sh|bash|zsh)\b|\.git")

_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
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
_NOTME_PROGS = {"notme", "notme.py"}
_PAUSE_OK = ([], ["on"], ["status"])
_MODE_OK = ("show", "dry-run")  # `notme mode` subcommands the agent may run
_PY_VALUE_OPTS = "WXQ"  # python short options that take a value (-W ignore, -Xutf8), skipped before -m or the script
# wrappers that give their command a pseudo-terminal or run it outside the session's process tree, which would pass
# the not-me CLI's human check: blocked around notme
_PTY_PROGS = {"script", "expect", "unbuffer"}
_ORPHAN_PREFIX = {"nohup", "setsid", "unbuffer"}
_ORPHAN_PROGS = {"disown", "daemon", "start-stop-daemon", "systemd-run", "at", "batch"}
# commands that copy or search a directory recursively: one that names a directory holding the not-me home reaches it
_TOKEN_COPIERS = {"cp", "mv", "ln", "rsync", "tar", "zip", "scp", "install"}
# git commands that are built in (git never runs an alias of these names); any other subcommand is looked up as an
# alias, and an external git-<name> program is outside the guard's view
_GIT_BUILTINS = set("""
add am annotate apply archive bisect blame branch bundle cat-file check-attr check-ignore check-mailmap
check-ref-format checkout checkout-index cherry cherry-pick citool clean clone column commit commit-graph commit-tree
config count-objects credential describe diagnose diff diff-files diff-index diff-tree difftool fast-export
fast-import fetch fetch-pack filter-branch fmt-merge-msg for-each-ref for-each-repo format-patch fsck gc
get-tar-commit-id grep gui hash-object help hook index-pack init interpret-trailers log ls-files ls-remote ls-tree
mailinfo mailsplit maintenance merge merge-base merge-file merge-index merge-tree mergetool mktag mktree
multi-pack-index
mv name-rev notes pack-objects pack-refs patch-id prune prune-packed pull push range-diff read-tree rebase reflog
remote repack replace request-pull rerere reset restore rev-list rev-parse revert rm send-email send-pack shortlog
show show-branch show-index show-ref sparse-checkout stash status stripspace submodule switch symbolic-ref tag
unpack-file unpack-objects update-index update-ref update-server-info var verify-commit verify-pack verify-tag
version whatchanged worktree write-tree
""".split())
# git config keys that change where git pushes or what it pushes (and aliases, which can hide a push): set through
# git -c, --config-env or a git config write, they block
_PUSH_KEY = re.compile(r"^(?:remote\..+\.(?:url|pushurl|push|mirror)|url\..+\.(?:insteadof|pushinsteadof)"
                       r"|remote\.pushdefault|branch\..+\.(?:remote|pushremote|merge)"
                       r"|push\.(?:default|recursesubmodules)"
                       r"|include\.path|includeif\..+\.path|alias\..+)$")
# variables that change which repository or config git uses, or where it finds ~/.gitconfig
_GIT_ENV_NAMES = r"(GIT_DIR|GIT_WORK_TREE|GIT_COMMON_DIR|GIT_NAMESPACE|GIT_CONFIG[A-Z0-9_]*|HOME|XDG_CONFIG_HOME)"
# set by assignment, ${NAME:=value}, or a builtin that sets a variable by name (export, read, printf -v, ...)
_GIT_ENV = re.compile(r"(?<![A-Za-z0-9_$])" + _GIT_ENV_NAMES + r"(?:\[[^\]]*\])?\s*\+?=|\$\{" + _GIT_ENV_NAMES
                      + r":?=|(?<![A-Za-z0-9_-])(?:export|declare|typeset|local|readonly|read|mapfile|readarray|unset"
                        r"|printf)(?![A-Za-z0-9_-])[^;&|\n]*?(?<![A-Za-z0-9_$])" + _GIT_ENV_NAMES
                      + r"(?![A-Za-z0-9_])")
# programs whose arguments name git or gh as data, not as a command to run
_NAMES_ONLY = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "which", "type", "whereis", "man", "command", "hash",
               "brew", "apt", "apt-get", "dnf", "yum", "pacman", "winget", "choco", "scoop", "npm", "pip", "pip3",
               "pipx", "test", "[", "[[", "complete", "compgen", "unalias", "alias"}
_GH_READ_VERBS = {"view", "list", "diff", "checks", "search", "browse", "clone", "download", "watch", "checkout",
                  "status", ""}
_GH_READ_GROUPS = {"config", "help", "completion", "search", "version", "status", "auth", "alias", "extension",
                   "ext", "extensions", "gist", "browse", "codespace", "attestation", "gpg-key", "ssh-key"}
_GH_GROUPS = {"pr", "issue", "repo", "release", "label", "run", "workflow", "secret", "variable", "cache",
              "project", "ruleset", "org"}
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
    rules: Tuple[str, ...] = ()  # for OPAQUE: the rules it may be reported under, in order
    no_relax: bool = False  # the fix is to rewrite the command, so the message leaves out how to relax the rule
    no_checkout: bool = False  # raised because the checkout (or its push URL) could not be resolved


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


# _norm results for the decision in progress (decide() sets and clears it): one command can name the same path
# thousands of times, and each miss costs file-system calls (realpath is slow on Windows). The disk does not change
# while one call is judged; outside decide() nothing is cached.
_norm_memo: Optional[Dict[Tuple[Any, str, str], str]] = None


def _norm(p: str, base: str) -> str:
    """The single path normaliser: unquote, expand ~, make absolute against base, resolve, normcase. Every
    comparison normalises both sides with it (then _under compares whole components), so on Windows drive-letter
    case, "/" against "\\" and short names all compare equal."""
    memo = _norm_memo
    key = (_path, str(p), str(base))
    if memo is not None and key in memo:
        return memo[key]
    q = _path.expanduser(p.replace('"', "").replace("'", ""))
    if not _path.isabs(q):
        q = _path.join(base, q)
    try:
        out = _path.normcase(_path.normpath(_resolve(q)))
    except (OSError, ValueError):  # a NUL byte or a name the OS refuses: compare it as typed
        out = _path.normcase(_path.normpath(q))
    if memo is not None:
        memo[key] = out
    return out


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


def _unresolved(word: str) -> bool:
    """The shell fills in part of this word ($VAR, $(...), backticks), so its value is not known here."""
    return "$" in word or "`" in word


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


def risky_text(text: str) -> bool:
    """text, read as opaque, holds a word that would block: used by the hook when it cannot decide a call at all."""
    return any(_SELF_TEXT.search(v) or _NOTME_TEXT.search(v) or any(match(v) for match, _ in _DANGER)
               for v in _spellings(text))


class Unparsed(ValueError):
    """The command cannot be split into words with confidence: the caller blocks it."""


class _Word(str):
    """A word with its quoting removed. `glob` and `brace` mark an unquoted glob or {a,b} brace expansion.
    `raw` is the word with its unquoted backslashes kept, the way a Windows path is typed (C:\\Users\\x)."""
    glob = False
    brace = False
    quoted = False
    raw = ""


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
        cmd.redirs.append((op, target))

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
        escaped: Set[int] = set()  # indexes in out of the characters an unquoted backslash escaped
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
                    escaped.add(len(out))
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
        w.raw = "".join("\\" + p if k in escaped else p for k, p in enumerate(out)) if escaped else str(w)
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
    """False for calls no rule can care about (cheap, no processes): Bash commands that mention nothing the guard
    judges, and MCP tools other than GitHub ones."""
    tool = event.get("tool_name")
    if isinstance(tool, str) and tool.startswith("mcp__"):
        return bool(_MCP_TOOL.match(tool))
    if tool != "Bash":
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
    # a backslash is a path separator on Windows (C:\Users\x): it alone needs the full check, even though the
    # pattern is matched with backslashes removed so that g\it still reads as git
    return "\\" in text or bool(_PREFILTER.search(text.replace("'", "").replace('"', "").replace("\\", "")))


# --- finding the checkout without git ------------------------------------------


class _Ambiguous(Exception):
    pass


def find_checkout(start: str) -> Optional[Tuple[str, Path, Path]]:
    """(toplevel, git dir, common dir) by walking up for .git; None outside git.

    Raises _Ambiguous when a .git file cannot be read as `gitdir: <path>`.
    """
    here = Path(_path.realpath(_path.abspath(start)))
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
            return str(d), Path(_path.realpath(gd)), Path(_path.realpath(common))
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
    if block.rule in RULES and not block.no_relax:
        lines.append(f"Relax this rule (not recommended): set guard.{block.rule}: off and "
                     f"confirm_guard_off: [{block.rule}] in {CONFIG_HINT}")
    lines.append("See effective config: /not-me:config")
    return "\n".join(lines)


# --- decision --------------------------------------------------------------

Blk = Callable[..., Block]


def _blk(shown: str) -> Blk:
    def blk(rule: str, reason: str, hint: Optional[str] = None, target: Optional[str] = None,
            suggest: bool = False, rules: Tuple[str, ...] = ()) -> Block:
        return Block(rule, reason, target=target, command=shown, hint=hint, suggest_allow=suggest, rules=rules)
    return blk


def decide(event: Mapping[str, Any], ctx: GuardContext) -> Decision:
    global _norm_memo
    if _norm_memo is not None:
        return _decide(event, ctx)
    _norm_memo = {}
    try:
        return _decide(event, ctx)
    finally:
        _norm_memo = None


def _decide(event: Mapping[str, Any], ctx: GuardContext) -> Decision:
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
    for block in cands:
        if ctx.only_self and block.rule != SELF:
            continue
        if block.rule == OPAQUE:
            enabled = [r for r in (block.rules or RULES) if ctx.rules.get(r, True)]
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
    b = blk("other_repo", f"could not tell which repo it targets ({why})", fix)
    b.no_checkout = True
    return b


_READ_TOOLS = ("Read", "NotebookRead", "LS", "Grep", "Glob")


# The not-me home's private files, as (name, why, hint). The agent may neither read nor write the secrets (the
# operator token and the live-mode epoch); the shell checks also cover the live marker, which the agent may not
# write (the Read tool may still read it).
_HOME_SECRETS = ((OPERATOR_TOKEN, TOKEN_WHY, TOKEN_HINT), (LIVE_EPOCH_NAME, LIVE_EPOCH_WHY, LIVE_HINT))
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
        if any(fnmatch.fnmatchcase(c, q) for c in g[:2] for q in pats):
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


def _edit_results(tool: str, tin: Mapping[str, Any], current: Optional[str]) -> List[str]:
    """Every text an Edit or MultiEdit may leave: applied to the file as stored and, for a file with CRLF line
    endings (as written on Windows), to its text with LF endings, which is how Claude Code matches old_string.
    Empty when no form can be worked out."""
    forms = [current] if current is None or "\r\n" not in current else [current, current.replace("\r\n", "\n")]
    out = [_after_edit(tool, tin, f) for f in forms]
    return [r for r in out if r is not None]


def _global_mode(tool: str, tin: Mapping[str, Any], full: str, blk: Blk) -> Iterator[Block]:
    """An early, friendly refusal (not the security boundary: live mode also needs the token-signed marker, which
    no file edit can make). An agent Write/Edit/MultiEdit of the global project.md is allowed only when, parsed as
    the config loader parses it, the result says `mode: dry-run`, or has no mode and had none before. Anything else
    (live, null, any other value, a result that does not parse, an edit that cannot be worked out) is blocked."""
    current = _read_text(full)
    try:
        afters = [_mode_of(text) for text in _edit_results(tool, tin, current)] or [_UNKNOWN]
    except Exception:
        afters = [_UNKNOWN]
    if all(a == "dry-run" or (a is _ABSENT and _mode_of(current) is _ABSENT) for a in afters):
        return
    yield blk(SELF, "only a person turns live mode on" + ("" if _UNKNOWN not in afters else
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
        yield blk(SELF, ALLOW_WHY, ALLOW_HINT)
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
    """GitHub MCP tools: read-only names pass; anything else must name an allowed repo, and merging is a human
    action. Other MCP tools are not judged."""
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


# --- Bash ----------------------------------------------------------------------


class _State:
    """Per-command state: the hook's real working directory, the command's text (code without here-document
    bodies, and raw with them), the branch a checkout moved to, and memoised lookups."""

    def __init__(self, cwd: str, parsed: Parsed, raw: str, depth: int = 0):
        self.dir = cwd
        self.depth = depth
        self.text = parsed.code + "\n" + parsed.code.replace("\\\n", "")
        self.raw = raw
        self.background = bool(_BACKGROUND.search(parsed.code))
        self.switched: Dict[str, str] = {}  # checkout dir -> the branch a checkout in this command moved to
        self.memo: Dict[Tuple[str, ...], Any] = {}

    def once(self, key: Tuple[str, ...], fn: Callable[[], Any]) -> Any:
        if key not in self.memo:
            self.memo[key] = fn()
        return self.memo[key]

    @cached_property
    def plain_text(self) -> str:
        return _unshell(self.text)

    @cached_property
    def plain_raw(self) -> str:
        return _unshell(self.raw)


def _bash(cmd: str, cwd: str, ctx: GuardContext, depth: int = 0) -> Iterator[Block]:
    """Judge every simple command in cmd; a command the lexer cannot read is judged as opaque text. When a word
    held an unquoted backslash, every command is judged again with those backslashes kept: bash drops them
    (C:\\Users\\x reads as C:Usersx) but a Windows path is typed with them, so either form reaching a guarded path
    blocks."""
    try:
        parsed = lex(cmd)
    except Unparsed as exc:
        yield from _opaque(cmd, _blk(_short(cmd)), f"the guard cannot parse this command ({exc})")
        return
    for p in (parsed, _raw_variant(parsed)):
        if p is None:
            continue
        st = _State(cwd, p, cmd, depth)
        for c in p.commands:
            yield from _command(c, st, ctx)


def _raw_word(w: str) -> str:
    raw = getattr(w, "raw", "")
    if not raw or raw == w:
        return w
    out = _Word(raw)
    out.glob, out.brace, out.quoted, out.raw = w.glob, w.brace, w.quoted, raw  # type: ignore[attr-defined]
    return out


def _raw_variant(parsed: Parsed) -> Optional[Parsed]:
    """parsed with every word in its backslashes-kept form, or None when no word differs."""
    if not any(getattr(w, "raw", w) != w for c in parsed.commands for w in c.words + [t for _, t in c.redirs]):
        return None
    commands = [SimpleCommand([_raw_word(w) for w in c.words], c.raw, c.span, c.bodies,
                              [(op, _raw_word(t)) for op, t in c.redirs], c.nested) for c in parsed.commands]
    return Parsed(commands, parsed.subs, parsed.code, parsed.funcdef)


def _opaque(text: str, blk: Blk, what: str, inline: bool = True) -> Iterator[Block]:
    """Text the guard does not parse: allowed unless a spelling of it names a protected path (or, when it sits in
    the command itself, a not-me switch), which is self-protection, or holds a word in _DANGER, which blocks under
    the first of that word's rules that is on."""
    spellings = _spellings(text)
    for v in spellings:
        m = _SELF_TEXT.search(v) or (_NOTME_TEXT.search(v) if inline else None)
        if m:
            yield blk(SELF, f"{what}, and it names {_short(m.group(0), 40)}, which only the operator may touch or run",
                      "ask the operator")
            return
    for v in spellings:
        for match, rules in _DANGER:
            hit = match(v)
            if hit:
                yield blk(OPAQUE, f"{what}, and it holds `{_short(hit, 40)}`", _OPAQUE_HINT, rules=rules)
                return


def _body(text: str, st: _State, ctx: GuardContext, blk: Blk, what: str) -> Iterator[Block]:
    """A string a shell runs as a command (bash -c, eval, a here-document fed to a shell): judged as a command in
    its own right, or as opaque text past MAX_BODY_DEPTH."""
    if st.depth >= MAX_BODY_DEPTH:
        yield from _opaque(text, blk, f"{what} nests commands more than {MAX_BODY_DEPTH} deep")
        return
    yield from _bash(text, st.dir, ctx, st.depth + 1)


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


def _command(c: SimpleCommand, st: _State, ctx: GuardContext) -> Iterator[Block]:
    """Check one simple command."""
    seg = c.raw
    blk = _blk(_short(seg))
    yield from _path_checks(c, st, ctx, blk)
    t = list(c.words)
    seen: Set[str] = set()
    while t:
        head = _head(t[0])
        if _ASSIGN.match(t[0]) or head in _KEYWORDS:
            t = t[1:]
        elif head in _PREFIX_WORDS:
            seen.add(head)
            t = _drop_prefix(head, t[1:])
        else:
            break
    if not t:
        return
    wrapper = _wrapper(t, seen, st)
    if wrapper is not None:
        yield from _around_notme(wrapper, t, st, blk)
    notme_args = _notme_args(t)
    if notme_args is not None:
        yield from _cmd_notme(notme_args, blk)
        return
    word = t[0]
    if getattr(word, "glob", False) or getattr(word, "brace", False) or _unresolved(word) or word.startswith("="):
        yield from _opaque(seg, blk, f"the shell decides what '{_short(word, 40)}' runs")
        return
    head = _head(word)
    handler = _COMMANDS.get(head)
    if handler is not None:
        yield from handler(t, c, st, ctx, blk)
    elif word.startswith(("./", "../", ".\\", "..\\")) or word.endswith(SCRIPT_SUFFIXES):
        yield from _script_file(word, st, blk)
    elif _INTERP.match(_prog(word)):
        yield from _interp(_prog(word), list(t[1:]), c, st, ctx, blk)
    elif _prog(word) not in _NAMES_ONLY:
        # a program the guard does not know that is handed git or gh as a word (watch, flock, xcrun, mise exec):
        # judged as the git or gh command it would run
        k = next((k for k, a in enumerate(t[1:], 1) if _head(a) in ("git", "gh")), None)
        if k is not None:
            yield from _command(SimpleCommand(t[k:], " ".join(t[k:])), st, ctx)


# --- self-protection by path ------------------------------------------------------


def _expand_home(arg: str, ctx: GuardContext) -> str:
    """arg with $HOME / ${HOME} and $NOT_ME_HOME / ${NOT_ME_HOME} filled in (~ is left to _norm)."""
    for var in ("${HOME}", "$HOME"):
        arg = arg.replace(var, ctx.env.get("HOME") or _path.expanduser("~"))
    for var in ("${NOT_ME_HOME}", "$NOT_ME_HOME"):
        arg = arg.replace(var, str(ctx.home))
    return arg


# what a Bash word may not name: the home's private files and the allow list
_BASH_GUARDED = _HOME_GUARDED + ((ALLOWLIST_NAME, ALLOW_WHY, ALLOW_HINT),)
# shell text that leaves a path unresolved for the guard: $(...), backticks, ${...}, $VAR, {a,b} and a stray $
_UNRESOLVED = re.compile(r"\$\([^)]*\)?|`[^`]*`?|\$\{[^}]*\}?|\$[A-Za-z0-9_@*#?!-]+|\{[^}]*\}?|\$")
_HOME_PARAM = re.compile(r"\$\{(NOT_ME_HOME|HOME)(?![A-Za-z0-9_])[^}]*\}")


def _path_checks(c: SimpleCommand, st: _State, ctx: GuardContext, blk: Blk) -> Iterator[Block]:
    """Words and redirection targets that name a protected file in the not-me home (the operator token, the
    live-mode epoch and marker, the allow list), the home itself, or for a recursive copy
    or search a directory that holds it. Globs are matched as patterns and expanded for real."""
    core = _core(list(c.words))
    prog = _prog(core[0]) if core else ""
    recursive = prog in _TOKEN_COPIERS or prog == "rg" or (prog in ("grep", "egrep", "fgrep") and (
        "r" in _short_flags(core[1:]).lower() or "--recursive" in core[1:]))
    args: List[str] = []
    for w in c.words:
        if not w.startswith("-"):
            args.append(w)
        if "=" in w:
            args.append(w.split("=", 1)[1])
    args += [target for op, target in c.redirs if op not in ("<<", "<<-", "<<<")]
    for a in args:
        found = _protected(a, st, ctx, recursive)
        if found:
            yield blk(SELF, found[0], found[1])
            return


def _protected(arg: str, st: _State, ctx: GuardContext, recursive: bool) -> Optional[Tuple[str, str]]:
    """(why, hint) when arg names a protected file: by its name after a path separator (in any directory), by its
    real path, or through a glob; a part the shell fills in ($VAR, $(...), backticks, {a,b}) counts as * when the
    rest still pins the pattern near a protected file. For a recursive copy or search, a directory that holds the
    not-me home counts too."""
    a = _expand_home(arg.replace('"', "").replace("'", ""), ctx)
    a = _HOME_PARAM.sub(lambda m: str(ctx.home) if m.group(1) == "NOT_ME_HOME"
                        else (ctx.env.get("HOME") or _path.expanduser("~")), a)
    if not a or "\n" in a:
        return None
    guarded = st.once(("guarded",), lambda: _guarded(ctx, st.dir, _BASH_GUARDED))
    parts = [p.lower() for p in _PATH_SEP.split(a)]
    if len(parts) > 1:
        for (name, why, hint) in _BASH_GUARDED:
            if parts[-1] == name:
                return why, hint
    home = st.once(("home",), lambda: _norm(str(ctx.home), st.dir))

    def absolute(p: str, unknown_base: bool = False) -> str:
        p = _path.expanduser(p)  # a pattern that starts with an unresolved part may be anywhere
        return p if _path.isabs(p) or (unknown_base and p.startswith("*")) else _path.join(st.dir, p)

    def hit(path: str) -> Optional[Tuple[str, str]]:
        full = _norm(path, st.dir)
        g = next((g for g in guarded if _under(full, g[0])), None)
        if g is not None:
            return g[2], g[3]
        if recursive and _under(home, full):
            return HOME_WHY, TOKEN_HINT
        return None
    if _UNRESOLVED.search(a):
        pattern = _UNRESOLVED.sub("*", a)
        g = _pattern_hit(absolute(pattern, True), st.dir, guarded, recursive) if pattern.strip("*/\\.") else None
        return (g[2], g[3]) if g is not None else None
    if not _GLOB.search(a):
        return hit(a)
    full = absolute(a)
    g = _pattern_hit(full, st.dir, guarded, recursive)
    if g is not None:
        return g[2], g[3]
    try:
        for k, match in enumerate(glob.iglob(full, recursive=True)):
            if k >= GLOB_LIMIT:
                break
            found = hit(match)
            if found:
                return found
    except (OSError, ValueError, re.error):
        return None
    return None


# --- wrappers around notme ----------------------------------------------------------


def _wrapper(t: List[str], seen: Set[str], st: _State) -> Optional[str]:
    """The name of what gives this command a pseudo-terminal or runs it outside the session's process tree, or
    None."""
    prog, args = _prog(t[0]), t[1:]
    low = [a.lower() for a in args]
    if seen & _ORPHAN_PREFIX:
        return sorted(seen & _ORPHAN_PREFIX)[0]
    if prog in _PTY_PROGS or prog in _ORPHAN_PROGS:
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
    if prog == "launchctl" and {"submit", "bootstrap"} & set(low):
        return "launchctl"
    if prog == "crontab" and [a for i, a in enumerate(low) if a != "-u" and not (i and low[i - 1] == "-u")] != ["-l"]:
        return "crontab"
    if st.background:
        return "a background &"
    return None


def _around_notme(wrapper: str, t: List[str], st: _State, blk: Blk) -> Iterator[Block]:
    """A wrapper that hides the caller may not run the not-me CLI: named anywhere in the command, or in a file it
    is given."""
    why = (f"{wrapper} would run notme in a pseudo-terminal or a detached process, which passes the not-me CLI's "
           "human check")
    hint = "the operator runs this not-me step in their own terminal"
    if any(_NOTME_WORD.search(v) for v in (st.plain_raw, st.raw)):
        yield blk(SELF, why, hint)
        return
    for a in t[1:]:
        if a.startswith("-"):
            continue
        text = _read_script(_norm(a, st.dir), st)
        if text is not None and _NOTME_WORD.search(text):
            yield blk(SELF, f"{why} ({a} names it)", hint)
            return


def _read_script(path: str, st: Optional[_State] = None) -> Optional[str]:
    """The start of a regular file as text, or None (memoised per call when st is given)."""
    if st is not None:
        return st.once(("read", path), lambda: _read_script(path))
    try:
        if not _path.isfile(path):
            return None
        with open(path, "rb") as fh:
            return fh.read(SCRIPT_READ_LIMIT).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


@lru_cache(maxsize=4096)  # pure, and called for every word by several checks
def _prog(word: str) -> str:
    """Program name for matching: basename, backslashes removed, lowercased, without .exe."""
    low = _base(str(word).replace("\\", "")).lower()
    return low[:-4] if low.endswith(".exe") else low


# --- the not-me CLI -----------------------------------------------------------------


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
                    return None  # inline code: judged as opaque text
                module = rest or (t[k + 1] if k + 1 < len(t) else "")
                return (t[k + 1:] if rest else t[k + 2:]) if _is_notme_module(module) else None
            if a.startswith("-"):
                module = a[2:] if a.startswith("-m") and len(a) > 2 else (
                    t[k + 1] if a == "-m" and k + 1 < len(t) else None)
                if module is not None and _is_notme_module(module):
                    return t[k + 1:] if len(a) > 2 else t[k + 2:]
                if a in ("-c", "-m", "-e", "--eval"):
                    return None  # inline code: judged as opaque text
                continue
            return _notme_args(t[k:]) if _prog(a) in _NOTME_PROGS | {"notme-python"} else None
    return None


def _is_notme_module(name: str) -> bool:
    """`-m` names the not-me package or one of its modules (notme, notme.cli, ...), in any case or quoting."""
    low = _unshell(name).lower()
    return low == "notme" or low.startswith("notme.")


def _cmd_notme(args: List[str], blk: Blk) -> Iterator[Block]:
    """The not-me CLI's human switches: changing the allow list, turning a pause off, and going live."""
    plain = [_unshell(a) for a in args]
    pos = [a for a in plain if not a.startswith("-")]
    helping = bool({"--help", "-h"} & set(plain))
    if pos[:1] == ["allow-repo"] and pos[1:2] != ["list"] and not helping:
        yield blk(SELF, ALLOW_WHY, ALLOW_HINT)
    if "pause" in plain:
        rest = plain[plain.index("pause") + 1:]
        if rest not in _PAUSE_OK:
            yield blk(SELF, "only a person turns a not-me pause off", "leave the pause for the operator")
    if "mode" in pos and not helping:
        # anything but `mode show` or `mode dry-run` (live, an abbreviation, a typo) is the person's switch
        sub = pos[pos.index("mode") + 1:pos.index("mode") + 2]
        if not sub or sub[0] not in _MODE_OK:
            yield blk(SELF, "only a person turns live mode on", LIVE_HINT)


# --- shells, scripts and interpreters --------------------------------------------------


def _script_file(path: str, st: _State, blk: Blk) -> Iterator[Block]:
    """A script file runs text the guard does not parse: judged as opaque text (a file it cannot read is allowed)."""
    text = _read_script(_norm(path, st.dir), st)
    if text is not None:
        yield from _opaque(text, blk, f"{_short(path, 60)} is a script the guard does not parse", inline=False)


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


def _from_input(c: SimpleCommand, st: _State, ctx: GuardContext, blk: Blk, what: str,
                shell: bool = True) -> Iterator[Block]:
    """A shell or interpreter that reads its program from its input: a here-document or here-string is judged
    (as a command for a shell, as opaque text for an interpreter), a < file is read, and a pipe leaves only the
    whole command's text to judge."""
    if c.bodies:
        for b in c.bodies:
            yield from (_body(b, st, ctx, blk, what) if shell else _opaque(b, blk, what))
        return
    files = [target for op, target in c.redirs if op == "<"]
    for f in files:
        yield from _script_file(f, st, blk)
    if not files:
        yield from _opaque(st.raw, blk, f"{what} reads its program from a pipe")


def _cmd_shell(t, c, st, ctx, blk):
    head = _head(t[0])
    args = t[1:]
    if head in _SHELLS:
        bodies, script = _shell_bodies(head, args)
        if bodies is not None:
            for body in bodies:
                yield from _body(body, st, ctx, blk, f"{head} -c")
            return
    else:  # source and .
        script = next((a for a in args if a != "--"), None)
    if script is not None and script != "-":
        yield from _script_file(script, st, blk)
    else:
        yield from _from_input(c, st, ctx, blk, head)


def _cmd_eval(t, c, st, ctx, blk):
    # eval joins its words with spaces and runs the result as a command
    yield from _body(" ".join(t[1:]), st, ctx, blk, "eval")


# per interpreter family: the short option letters and long options that take inline code
_INLINE_SHORT = {"python": "c", "node": "ep", "perl": "eE", "ruby": "e", "php": "rB", "lua": "e", "julia": "eE",
                 "rscript": "e", "osascript": "e", "bun": "ep"}
_INLINE_LONG = {"--eval", "--print", "--command", "--code"}
_INTERP_VALUE = {"python": {"-W", "-X", "-Q"}, "node": {"-r", "--require", "--import", "--loader"},
                 "perl": {"-I", "-M", "-m"}, "ruby": {"-I", "-r"}}


def _interp_family(prog: str) -> str:
    if prog.startswith("py"):
        return "python"
    if prog.startswith(("powershell", "pwsh")):
        return "pwsh"
    return next((f for f in ("node", "perl", "ruby", "php", "lua", "julia", "rscript", "osascript", "bun", "deno")
                 if prog.startswith(f)), prog)


def _interp(prog: str, args: List[str], c: SimpleCommand, st: _State, ctx: GuardContext,
            blk: Blk) -> Iterator[Block]:
    """An interpreter runs code the guard does not parse: inline code (with the rest of its arguments) and a
    script file are judged as opaque text, and so is its input when it reads its program from there. A python -m
    module is not read (what it imports is out of view, like any program)."""
    family = _interp_family(prog)
    what = f"{prog} runs code the guard does not parse"
    if family == "pwsh":
        low = [a.lower() for a in args]
        if "-file" in low and low.index("-file") + 1 < len(args):
            yield from _script_file(args[low.index("-file") + 1], st, blk)
        else:
            yield from _opaque(" ".join(args), blk, what)
        return
    letters = _INLINE_SHORT.get(family, "")
    inline = (family == "deno" and args[:1] == ["eval"]) or any(
        a in _INLINE_LONG or a.split("=", 1)[0] in _INLINE_LONG
        or (letters and a.startswith("-") and not a.startswith("--") and set(a[1:]) & set(letters)) for a in args)
    if inline:
        yield from _opaque("\n".join([" ".join(args)] + c.bodies), blk, what)
        return
    takes_value = _INTERP_VALUE.get(family, set())
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] != "-":
        a = args[i]
        if family == "python" and (a == "-m" or (a.startswith("-") and not a.startswith("--") and "m" in a[1:])):
            return  # a module run
        i += 2 if a in takes_value else 1
    rest = args[i:]
    if family == "deno" and rest[:1] == ["run"]:
        rest = _positionals(rest[1:])
    if rest and rest[0] != "-":
        yield from _script_file(rest[0], st, blk)
    else:
        yield from _from_input(c, st, ctx, blk, prog, shell=False)


def _wrapped(takes_value: Set[str], stop: Tuple[str, ...] = (), adds_input: bool = True):
    """Handler for xargs, parallel and sudo: judge the command they run; xargs and parallel add words from their
    input, so that command's text is also judged as opaque."""
    def handler(t, c, st, ctx, blk):
        rest = _skip_options(t[1:], takes_value)
        for k, a in enumerate(rest):
            if a in stop:
                rest = rest[:k]
                break
        if not rest:
            if _prog(t[0]) == "parallel":
                yield from _opaque(st.raw, blk, "parallel runs its input lines as commands")
            return
        yield from _command(SimpleCommand(rest, " ".join(rest)), st, ctx)
        if adds_input:  # the words come from its input: the whole command (here-documents too) is the text to judge
            yield from _opaque(st.raw, blk, f"{_prog(t[0])} adds words from its input")
    return handler


def _cmd_script(t, c, st, ctx, blk):
    """script runs `-c <command>` (util-linux) or the command after its log file (BSD) in a pseudo-terminal."""
    args = t[1:]
    for k, a in enumerate(args):
        name, eq, val = a.partition("=")
        if a in ("-c", "--command") or (eq and name == "--command"):
            yield from _body(val if eq else (args[k + 1] if k + 1 < len(args) else ""), st, ctx, blk, "script -c")
            return
    rest = _positionals(_skip_options(args, {"-t", "-T", "-B", "-I", "-O", "-E", "-m"}))
    if len(rest) > 1:
        yield from _command(SimpleCommand(rest[1:], " ".join(rest[1:])), st, ctx)


def _cmd_find(t, c, st, ctx, blk):
    for k, a in enumerate(t):
        if a in _FIND_EXEC:
            sub: List[str] = []
            for w in t[k + 1:]:
                if w in (";", "\\;", "+", "\\"):
                    break
                sub.append(w)
            if sub:
                yield from _command(SimpleCommand(sub, " ".join(sub)), st, ctx)


def _cmd_rm(t, c, st, ctx, blk):
    args = t[1:]
    flags = _short_flags(args)
    if not ("r" in flags or "R" in flags or "--recursive" in args):
        return
    root = _norm(ctx.root, "/") if ctx.root else None
    for target in _positionals(args):
        full = _norm(target, st.dir)
        hits_root = root is not None and (_under(root, full) or (target in ("*", ".*") and _norm(st.dir, "/") == root))
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


def _cmd_git(t, c, st, ctx, blk, depth: int = 0):
    d = st.dir
    c_keys: List[str] = []
    redirect: Optional[str] = None
    var_opt: Optional[str] = None  # the first -C, --git-dir or --work-tree whose path the shell fills in
    var_redirect: Optional[str] = None  # the first --git-dir or --work-tree whose path the shell fills in
    i = 1
    while i < len(t) and t[i].startswith("-"):
        a = t[i]
        name, eq, val = a.partition("=")
        nxt = t[i + 1] if i + 1 < len(t) else ""
        if (a == "-C" or name in ("--git-dir", "--work-tree")) and _unresolved(val if eq else nxt):
            var_opt = var_opt or name
            if name != "-C":
                var_redirect = var_redirect or name
        if a == "-C":  # as typed: with unquoted backslashes bash would hand git a path that does not exist
            d = _norm(getattr(nxt, "raw", "") or nxt or ".", d)
            i += 2
        elif a == "-c":
            c_keys.append(nxt.split("=", 1)[0])
            i += 2
        elif name == "--config-env":
            c_keys.append((val if eq else nxt).split("=", 1)[0])
            i += 1 if eq else 2
        elif name in ("--git-dir", "--work-tree"):
            redirect = name
            i += 1 if eq else 2
        elif name in ("--namespace", "--super-prefix"):
            i += 1 if eq else 2
        else:
            i += 1
    sub, args = (t[i] if i < len(t) else ""), t[i + 1:]
    if _unresolved(sub) or any(getattr(w, "brace", False) or getattr(w, "glob", False) for w in t[:i + 1]) or \
            any(getattr(w, "brace", False) for w in args):
        yield from _opaque(c.raw, blk, "the shell fills in part of this git command")
        return
    key = next((k for k in c_keys if _PUSH_KEY.match(k.lower())), None)
    if key is not None:
        yield blk("other_repo", f"git -c {_short(key, 40)} can change where or what git pushes",
                  "run git without it; ask the operator to change git config")
        return
    if sub and sub not in _GIT_BUILTINS:
        alias = st.once(("alias", d, sub), lambda: ctx.git(["config", "--get", f"alias.{sub}"], d))
        if alias:
            yield from _git_alias(alias, sub, args, t[:i], c, st, ctx, blk, depth)
        if var_opt is not None:
            # the alias that runs lives in the repo the shell names, which may not be the one looked up here: after
            # any rules the alias found here raises (decide takes the first enabled one), fail closed
            yield _var_path(blk, var_opt)
        return  # an external git-<name> program is outside the guard's view
    redirect = redirect or st.once(("git-env",), lambda: (lambda m: _short(m.group(0), 40) if m else None)(
        _GIT_ENV.search(st.plain_text)))
    if redirect and sub in _GIT_RULES and sub not in ("checkout", "switch"):
        if var_redirect is not None:
            yield _var_path(blk, var_redirect)
        else:
            yield blk("other_repo", f"{redirect} changes which repository or config git uses",
                      "run git without it, in the checkout it should change")
        return
    handler = _GIT_RULES.get(sub)
    if handler is not None:
        for b in handler(_GitCall(ctx, st, blk, d, sub, args)):
            # the guard looked for a checkout literally named $W: say so rather than blame the repo (blocks that do
            # not come from the unresolved checkout keep their own message)
            if var_opt is not None and b.no_checkout:
                b = _var_path(blk, var_opt)
            yield b
        if var_opt is not None and not _git_reads(sub, args):
            # the handler judged the checkout _norm made of the path ("$W" names nothing, "$W/.." names this one),
            # not the one the shell will pick: after its own rules, fail closed on any write
            yield _var_path(blk, var_opt)


# git branch options that only list; git accepts abbreviated long options, so anything not spelled out here is a write
_BRANCH_READ_SHORT = set("arvliq")
_BRANCH_READ_LONG = {"--list", "--all", "--remotes", "--verbose", "--show-current", "--color", "--no-color", "--format",
                     "--sort", "--ignore-case", "--column", "--no-column", "--abbrev", "--no-abbrev", "--quiet",
                     "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--omit-empty"}


def _branch_lists(args: List[str]) -> bool:
    opts = [a for a in args if a.startswith("-")]
    if not set(_short_flags(opts)) <= _BRANCH_READ_SHORT or \
            any(a.startswith("--") and a.split("=", 1)[0] not in _BRANCH_READ_LONG for a in opts):
        return False
    return not _positionals(args) or "--list" in args or "l" in _short_flags(opts)


def _git_reads(sub: str, args: List[str]) -> bool:
    """A subcommand with a _GIT_RULES handler, used in a form that changes nothing git would push or keep."""
    # checkout and switch are not reads here: the guard would track the branch of the wrong checkout, and a later
    # commit or push in the same command would be judged on that drifted branch
    if sub == "config":
        return _config_reads(args)
    if sub == "remote":
        return _remote_verb(args) in ("", "show", "get-url")
    if sub == "submodule":
        return next((a for a in args if not a.startswith("-")), "") in ("", "status", "summary")
    if sub == "clean":  # -e (or any abbreviation of --exclude) takes a value and -- starts pathspecs: no -n after them
        opts = args[:args.index("--")] if "--" in args else args
        flags = _short_flags(opts)
        if "e" in flags or any(len(n) > 2 and "--exclude".startswith(n) for n in (a.split("=", 1)[0] for a in opts)):
            return False
        return "n" in flags or "--dry-run" in opts
    if sub == "branch":
        return _branch_lists(args)
    return False


def _var_path(blk: Blk, opt: str) -> Block:
    fix = "write the path out in full (`git -C /abs/path <sub> ...`)" if opt == "-C" else \
        f"drop {opt} and run `git -C /abs/path <sub> ...` with the path written out in full"
    # the unknown checkout may be another repo or on a protected branch: reported under the first of those rules on
    b = blk(OPAQUE, f"the guard does not expand shell variables in a `git {opt}` path",
            fix + "; a rewritten command is checked afresh, so this is not a retry",
            rules=("other_repo", "protected_branches"))
    b.no_relax = True
    return b


def _git_alias(alias: str, sub: str, args: List[str], prefix: List[str], c: SimpleCommand, st: _State,
               ctx: GuardContext, blk: Blk, depth: int) -> Iterator[Block]:
    """A git alias: a plain one is judged as the git command it stands for; a shell alias (!...) or one nested too
    deep is opaque text."""
    what = f"git {sub} is an alias"
    if alias.startswith("!") or depth >= MAX_BODY_DEPTH:
        yield from _opaque(alias + " " + " ".join(args), blk, what)
        return
    try:
        parsed = lex(alias)
    except Unparsed:
        parsed = None
    if parsed is None or len(parsed.commands) != 1 or parsed.subs:
        yield from _opaque(alias + " " + " ".join(args), blk, what)
        return
    words = list(prefix) + list(parsed.commands[0].words) + list(args)
    yield from _cmd_git(words, c, st, ctx, blk, depth + 1)


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
    elif len(pos) == 1 and "--" not in c.args and not _path.exists(_path.join(c.d, pos[0])):
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


def _remote_verb(args: List[str]) -> str:
    """The git remote subcommand after every leading option ("" for a plain listing): git takes -vv, -v -v and
    abbreviations such as --verb, so no option spelling may hide the verb."""
    return next((a for a in args if not a.startswith("-")), "")


def _git_remote(c: _GitCall) -> Iterator[Block]:
    verb = _remote_verb(c.args)
    if verb in ("add", "set-url", "rename"):
        yield c.blk("other_repo", f"git remote {verb} can redirect pushes", "ask the operator to change remotes")


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


def _git_config_cmd(c: _GitCall) -> Iterator[Block]:
    """Reads pass; a write may not set a key that changes where or what git pushes, or open the whole file."""
    if _config_reads(c.args):
        return
    opts = [a for a in c.args if a.startswith("-")]
    pos = [a for a in c.args if not a.startswith("-")]
    if pos[:1] in (["set"], ["unset"], ["rename-section"], ["remove-section"], ["edit"]):
        opts.append("--" + pos[0])
        pos = pos[1:]
    whole = {"-e", "--edit", "--rename-section", "--remove-section", "-f", "--file", "--blob"} & set(opts)
    if whole or (pos and _PUSH_KEY.match(pos[0].lower())):
        what = sorted(whole)[0] if whole else pos[0]
        yield c.blk("other_repo", f"git config {_short(what, 40)} can change where or what git pushes",
                    "ask the operator to change git config")


_SEQUENCER = ("--abort", "--quit", "--continue", "--skip")


def _git_move(c: _GitCall) -> Iterator[Block]:
    """merge, rebase, cherry-pick and am while on a protected branch; the commands rebase --exec runs."""
    if c.sub == "rebase":
        for k, a in enumerate(c.args):
            name, eq, val = a.partition("=")
            if a == "-x" or (not eq and _long(name, "--exec")):
                body = c.args[k + 1] if k + 1 < len(c.args) else ""
            elif a.startswith("-x") or (eq and _long(name, "--exec")):
                body = val if eq else a[2:]
            else:
                continue
            yield from _body(body, c.st, c.ctx, c.blk, "git rebase --exec")
    if any(a in _SEQUENCER for a in c.args):
        return
    b = _branch(c.ctx, c.st, c.d)
    if b in c.ctx.protected_branches:
        yield c.blk("protected_branches", f"git {c.sub} on the protected branch {b}",
                    "switch to a feature branch first; merging into it is a human action")


def _git_submodule(c: _GitCall) -> Iterator[Block]:
    """git submodule foreach runs a shell command in every submodule."""
    if "foreach" in c.args:
        rest = c.args[c.args.index("foreach") + 1:]
        yield from _body(" ".join(a for a in rest if a != "--recursive"), c.st, c.ctx, c.blk, "git submodule foreach")


def _git_reset(c: _GitCall) -> Iterator[Block]:
    if not any(_long(a, "--hard") or a == "--hard" for a in c.args):
        return
    b = _branch(c.ctx, c.st, c.d)
    if b in c.ctx.protected_branches:
        yield c.blk("protected_branches", f"git reset --hard on the protected branch {b} discards work",
                    "switch to a feature branch first; ask the operator to reset protected branches")


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


def _long(name: str, *options: str) -> bool:
    """name is one of options or an abbreviation git would accept for one (git takes any unambiguous prefix)."""
    return len(name) > 3 and any(o.startswith(name) for o in options)


def _git_push(c: _GitCall) -> Iterator[Block]:
    ctx, st, blk, d = c.ctx, c.st, c.blk, c.d
    force = delete = mirror = all_ = recurse = False
    positional: List[str] = []
    repo_opt: Optional[str] = None
    args = list(c.args)
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        name, eq, val = a.partition("=")
        if a == "--":
            positional += args[k:]
            break
        if a.startswith("--no-"):
            continue
        if a.startswith("--"):
            if _long(name, "--force", "--force-with-lease", "--force-if-includes"):
                force = True
            elif _long(name, "--delete"):
                delete = True
            elif _long(name, "--mirror"):
                mirror = True
            elif _long(name, "--all", "--branches"):
                all_ = True
            elif _long(name, "--recurse-submodules") and (val if eq else "on-demand") not in ("no", "check"):
                recurse = True
            elif _long(name, "--repo"):
                repo_opt = val if eq else (args[k] if k < len(args) else "")
                k += 0 if eq else 1
            elif not eq and _long(name, "--push-option", "--receive-pack", "--exec"):
                k += 1  # the value
            continue
        if a.startswith("-") and len(a) > 1:
            force = force or "f" in a[1:]
            delete = delete or "d" in a[1:]
            if a == "-o":
                k += 1
            continue
        positional.append(a)
    remote = repo_opt if repo_opt is not None else (positional[0] if positional else None)
    refspecs = positional if repo_opt is not None else positional[1:]

    if force or any(r.startswith("+") for r in refspecs):
        yield blk("force_push", "force pushes rewrite published history",
                  "push new commits on top instead; ask the operator if a rewrite is really needed")
    if delete or any(r.startswith(":") for r in refspecs):
        yield blk("force_push", "this push deletes a remote branch", "leave remote branches for a person to delete")
    if mirror:
        yield blk("protected_branches", "git push --mirror overwrites every remote ref, protected branches too",
                  "push one feature branch")
    if all_ and ctx.protected_branches:
        yield blk("protected_branches", "git push --all would push protected branches", "push one feature branch")
    if recurse or (_git_config(ctx, st, d, "push.recurseSubmodules") or "").strip().lower() not in (
            "", "no", "check", "false"):
        yield blk("other_repo", "this push also pushes submodule repos", "push without recursing into submodules")
    current = _branch(ctx, st, d)
    for r in refspecs or [""]:
        if _unresolved(r):
            yield blk("protected_branches", f"the shell fills in the refspec {_short(r, 40)}, so the branch it "
                                            "pushes to cannot be checked", "name the branch: git push origin <branch>")
            break
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
        b = blk("other_repo", f"remote '{remote}' has no push URL here",
                "push to a remote that exists and points at an allowed repo")
        b.no_checkout = True
        yield b
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
    "merge": _git_move, "rebase": _git_move, "cherry-pick": _git_move, "am": _git_move, "reset": _git_reset,
    "branch": _git_branch, "clean": _git_clean, "submodule": _git_submodule,
}

# --- gh --------------------------------------------------------------------


def _cmd_gh(t, c, st, ctx, blk):
    t = list(t)
    lead: List[str] = []  # -R/--repo before the subcommand applies to it
    while len(t) > 1 and (t[1] in ("-R", "--repo") or t[1].startswith("--repo=") or
                          (t[1].startswith("-R") and len(t[1]) > 2)):
        n = 2 if t[1] in ("-R", "--repo") else 1
        lead += t[1:1 + n]
        t = [t[0]] + t[1 + n:]
    if lead:
        t = t[:3] + lead + t[3:]
    group = t[1] if len(t) > 1 else ""
    if any(_unresolved(w) or getattr(w, "brace", False) or getattr(w, "glob", False) for w in t[1:3]) or \
            any(getattr(w, "brace", False) for w in t):
        yield from _opaque(c.raw, blk, "the shell fills in part of this gh command")
        return
    if group in ("", "--version", "--help", "-h") or group.startswith("-") or group in _GH_READ_GROUPS:
        return
    if group == "api":
        yield from _gh_api(t, c.raw, st, ctx, blk)
    elif group in _GH_GROUPS:
        yield from _gh_write(t, ctx, blk)


def _gh_write(t: List[str], ctx: GuardContext, blk: Blk) -> Iterator[Block]:
    group, verb = t[1], (t[2] if len(t) > 2 else "")
    what = f"gh {group} {verb}".strip()
    if group in _GH_DESTRUCTIVE and (_GH_DESTRUCTIVE[group] is None or verb in _GH_DESTRUCTIVE[group]):
        yield blk("destructive_git", f"{what} changes settings, secrets or deletes data; that is a human action",
                  "ask the operator to do this by hand")
    if group == "pr" and verb in ("merge", "close") and any(
            a == "--delete-branch" or (a.startswith("--delete-branch=") and a.split("=", 1)[1].lower() != "false")
            or (a.startswith("-") and not a.startswith("--") and "d" in a[1:]) for a in t[3:]):
        yield blk("force_push", f"{what} --delete-branch deletes the remote branch, a protected one too",
                  f"run {what} without --delete-branch; a person deletes branches by hand")
    if verb in _GH_READ_VERBS:
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


def _gh_api(t: List[str], seg: str, st: _State, ctx: GuardContext, blk: Blk) -> Iterator[Block]:
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
        if inputs or len(queries) != 1 or _unresolved(queries[0]) or queries[0].startswith("@"):
            yield blk("other_repo", "gh api graphql without one literal query cannot be checked",
                      "pass the query inline with -f query='...'")
            return
        atfile = any("@" in f for f in fields)
        if (mutation or atfile or inputs) and BOARD_MARKER not in seg:
            why = "a mutation" if mutation else ("an @file field" if atfile else "--input")
            yield blk("other_repo", f"gh api graphql with {why} cannot be checked",
                      "use gh pr/issue commands with --repo, or the REST API under repos/owner/name")
        return
    method = (values["method"][-1] if values["method"] else ("POST" if fields or inputs else "GET")).upper()
    if method in ("GET", "HEAD"):
        return
    if ".." in ep or "//" in ep or "\\" in ep or re.search(r"%(?:2e|2f|5c)", ep, re.I):
        yield blk("other_repo", f"gh api {method} {_short(ep, 60)}: dot segments or encoded separators can reach "
                                "another repo", "spell out repos/owner/name/... plainly")
        return
    m = _REPO_PATH.match(ep)
    repo_root = bool(m) and not ep[m.end():].strip("/")
    if method == "DELETE" or (repo_root and method == "PATCH") or _PROTECTION.search(ep) or _SECRETS.search(ep) \
            or _RULESETS.search(ep):
        yield blk("destructive_git", f"gh api {method} {ep}: settings, secrets and deletion are human actions",
                  "ask the operator to do this by hand")
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


_COMMANDS: Dict[str, Callable[..., Iterator[Block]]] = {
    "rm": _cmd_rm, "git": _cmd_git, "gh": _cmd_gh, "find": _cmd_find, "eval": _cmd_eval, "script": _cmd_script,
    "xargs": _wrapped(_XARGS_VALUE), "parallel": _wrapped(_PARALLEL_VALUE, (":::", "::::")),
    "sudo": _wrapped(_SUDO_VALUE, adds_input=False), "doas": _wrapped(_SUDO_VALUE, adds_input=False),
    **{s: _cmd_shell for s in (*_SHELLS, "source", ".")},
}


# --- log and self-test -----------------------------------------------------


LOG_INPUT_MAX = 2000  # characters of the blocked input kept in a guard log line
LOG_TRUNCATED = "...[truncated]"
REDACTED = "[redacted]"
_SECRET_FILE_MAX = 4096  # bytes read from a secret file; the token, marker and epoch are far shorter
_SECRET_MIN = 8  # a secret file value shorter than this is not searched for (it would redact ordinary text)
# the one field that says what a tool call touched; Write and Edit bodies are never logged
_LOG_FIELD = {"Bash": "command", "Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
              "Read": "file_path", "NotebookEdit": "notebook_path", "NotebookRead": "notebook_path"}
_LOG_BODY_KEYS = frozenset({"content", "old_string", "new_string", "edits", "new_source", "body"})
# The log patterns run on what the model sent, inside the hook's timeout (a hook that times out does not block), so
# each one must be linear: a match may only start where a run of its leading characters starts (the lookbehinds),
# and every repeated part stops at a character that ends it.
# A redacted value (after NAME=, NAME+=, NAME[index]=, a secret flag) is one shell word: a run of adjacent
# double-quoted, $'...', single-quoted, $(...) without nesting, backquoted and unquoted (backslash-escaped) chunks.
# A quoted chunk also ends at the end of the text, so a value whose closing quote is missing or past a cut is still
# redacted. _LOG_ASSIGN is not the parser's _ASSIGN: that one decides what a command word is.
_LOG_WORD = (r"""(?:"(?:[^"\\]|\\.)*(?:"|\\?\Z)|\$'(?:[^'\\]|\\.)*(?:'|\\?\Z)|'[^']*(?:'|\Z)|\$\([^()]*(?:\)|\Z)"""
             r"""|`[^`]*(?:`|\Z)|\\.|[^\s;&|()<>`"'\\])+""")
_LOG_WORD_RE = re.compile(_LOG_WORD, re.DOTALL)
# What may come before a value: an assignment name, a flag (--password X, --token=X, -b X, glued -bX and -uX) or a
# here-string. Only the name is matched, never the value, so a harmless flag or assignment does not swallow a secret
# one after it or inside its quoted value; _redact_values reads the value.
_LOG_ASSIGN = re.compile(r"(?<![A-Za-z0-9_$])([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\[\]\s]*\])?\+?=")
_LOG_FLAG = re.compile(r"(?<![A-Za-z0-9_-])(?:(-[bu])(?=[^\s=-])|(--?[A-Za-z][A-Za-z0-9_-]*)(?:\s+|=))|<<<\s*")
_LOG_BODY_FLAGS = frozenset({"-b", "--body"})
_LOG_USER_FLAGS = frozenset({"-u", "--user"})
_GH_SECRET = re.compile(r"\bsecret\b")  # gh secret [-R o/r] set: set may follow a repo flag
_GH_SET = re.compile(r"\bset\b")
_USER_SHORT = re.compile(r"-[A-Za-z]*u")  # -u, or combined short flags ending in it (curl -sSu user:pass)
_SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|APIKEY|(?:^|_)KEY$|^NOT_ME_OPERATOR_CONFIRM$",
                          re.IGNORECASE)
_AUTH_HEADER = re.compile(r"(authorization:\s*(?:bearer|token|basic)\s+)[^\s\"']+", re.IGNORECASE)
# scheme://user:password@ keeps the user; a lone user part of 16 or more characters is taken for a token
_URL_PASSWORD = re.compile(r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*://[^@\s/:]+:)[^@\s/]+@")
_URL_TOKEN_USER = re.compile(r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*://)[^@\s/:]{16,}@")
# The input is cut before redacting so the work stays bounded: to _LOG_SCAN characters, and again to _LOG_SCAN plus
# _LOG_SCAN_MARGIN. Only what both redacted cuts agree on is kept, so a secret no longer than the margin that one cut
# splits (and so does not redact) never shows in part.
_LOG_SCAN_MARGIN = _SECRET_FILE_MAX + 256
_LOG_SCAN = LOG_INPUT_MAX + _LOG_SCAN_MARGIN


def _secret_values(ctx: GuardContext) -> List[str]:
    """The operator token, live-mode marker and epoch file values; read only when a block is logged."""
    homes = {ctx.home, ctx.allowlist_path.parent}
    found = []
    for home in homes:
        for name, _why, _hint in _HOME_GUARDED:
            try:
                with open(home / name, "rb") as fh:
                    value = fh.read(_SECRET_FILE_MAX).decode("utf-8", "replace").strip()
            except OSError:
                continue
            if len(value) >= _SECRET_MIN:
                found.append(value)
    return sorted(set(found), key=len, reverse=True)


def _secret_name(name: str) -> bool:
    """A variable, flag or argument key named like a token, key, secret or password (--api-key reads as API_KEY)."""
    return bool(_SECRET_NAME.search(name.lstrip("-").replace("-", "_")))


def _redact_values(text: str, names: "re.Pattern[str]", how: Callable[["re.Match[str]"], Optional[str]]) -> str:
    """Redact the shell word after each match of names that how marks "all" (the whole word) or "colon" (after its
    first colon, as in user:pass). A redacted value is consumed; a "colon" value with no colon is not, so a secret
    flag inside it is still judged, but later "colon" matches inside it are skipped. So no span is scanned twice for
    the same mode: linear."""
    out: List[str] = []
    pos = 0
    colon_seen = 0  # end of the last "colon" value that held no colon
    for m in names.finditer(text):
        if m.start() < pos:
            continue
        mode = how(m)
        if mode == "colon" and m.start() < colon_seen:
            continue
        word = _LOG_WORD_RE.match(text, m.end()) if mode else None
        if word is None:
            continue
        value = word.group(0)
        if mode == "colon" and ":" not in value:
            colon_seen = word.end()
            continue
        if mode == "all":
            out.append(text[pos:m.end()] + REDACTED)
        else:
            out.append(text[pos:m.end()] + value.split(":", 1)[0] + ":" + REDACTED)
        pos = word.end()
    out.append(text[pos:])
    return "".join(out)


def _redact(text: str, secrets: List[str]) -> str:
    """Replace known secrets, secret-named assignment values, auth header values, URL credentials and GitHub
    token shapes."""
    for value in secrets:
        text = text.replace(value, REDACTED)
    text = _redact_values(text, _LOG_ASSIGN, lambda m: "all" if _secret_name(m.group(1)) else None)
    secret = _GH_SECRET.search(text)
    secret_set = bool(secret and _GH_SET.search(text, secret.end()))

    def flag(m: "re.Match[str]") -> Optional[str]:
        name = m.group(1) or m.group(2)
        if name is None:  # a here-string: the body of gh secret set
            return "all" if secret_set else None
        if _secret_name(name) or (secret_set and name in _LOG_BODY_FLAGS):
            return "all"
        return "colon" if name in _LOG_USER_FLAGS or _USER_SHORT.fullmatch(name) else None

    text = _redact_values(text, _LOG_FLAG, flag)
    text = _AUTH_HEADER.sub(lambda m: m.group(1) + REDACTED, text)
    text = _URL_PASSWORD.sub(lambda m: m.group(1) + REDACTED + "@", text)
    text = _URL_TOKEN_USER.sub(lambda m: m.group(1) + REDACTED + "@", text)
    for pattern in redact.TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def _loggable(value: Any) -> Any:
    """A copy of a JSON-shaped tool input with body keys dropped and secret-named keys' values redacted, at every
    depth (push_files carries files[].content, a review comments[].body)."""
    if isinstance(value, dict):
        return {k: REDACTED if isinstance(k, str) and _secret_name(k) else _loggable(v)
                for k, v in value.items() if k not in _LOG_BODY_KEYS}
    if isinstance(value, (list, tuple)):
        return [_loggable(v) for v in value]
    return value


def _logged_input(ctx: GuardContext, block: Block, tool_input: Any) -> Optional[str]:
    """What was blocked, redacted then capped (redacting first means the cut cannot expose part of a secret). Long
    input is cut twice before redacting, to bound the work, and only the redacted text both cuts agree on is kept."""
    text: Optional[str] = None
    if isinstance(tool_input, dict):
        field = tool_input.get(_LOG_FIELD.get(block.tool, ""))
        if isinstance(field, str) and field:
            text = field
        else:
            text = json.dumps(_loggable(tool_input), sort_keys=True, separators=(",", ":"), default=str)
    elif isinstance(tool_input, str) and tool_input:
        text = tool_input
    elif block.command:
        text = block.command
    if text is None:
        return None
    secrets = _secret_values(ctx)
    if len(text) <= _LOG_SCAN:
        text = _redact(text, secrets)
        return text if len(text) <= LOG_INPUT_MAX else text[:LOG_INPUT_MAX] + LOG_TRUNCATED
    # a secret the shorter cut splits ends inside the longer one and is redacted there, so the two differ from where
    # it starts; one the longer cut splits starts past the shorter one, so the agreed prefix stops before it
    text = os.path.commonprefix([_redact(text[:_LOG_SCAN], secrets),
                                 _redact(text[:_LOG_SCAN + _LOG_SCAN_MARGIN], secrets)])
    return text[:LOG_INPUT_MAX] + LOG_TRUNCATED


def log_block(ctx: GuardContext, block: Block, tool_input: Any = None) -> None:
    """Append the block to the guard log in the default run-log root (none outside git), with the blocked input
    (the Bash command or the tool's path, else its arguments without bodies) redacted and capped."""
    root = log_root(ctx.common_dir)
    if root is None:
        return
    try:
        shown = _logged_input(ctx, block, tool_input)
    except Exception:  # noqa: BLE001 - logging never changes the decision
        shown = None
    runlog.guard_log_append(runlog.guard_log_path(root), {
        "tool": block.tool, "rule": block.rule, "reason": block.reason, "target": block.target, "input": shown})


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
