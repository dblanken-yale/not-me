"""The GitHub client. Every GitHub write goes through here and through the write gate.

It gathers and returns JSON; judgment stays with the model. All body and text
inputs are file paths, never shell arguments.
"""
from __future__ import annotations

import fnmatch
import json as _json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import writes
from .dryrun import Ledger
from .writes import Decision, GuardContext, WriteTarget

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

# (command, verb) pairs that only read; `search` and `status` take any arguments. Everything else is a write.
_READ_VERBS = {"issue": {"view", "list"}, "pr": {"view", "list", "diff", "checks"}, "repo": {"view"},
               "run": {"view", "list"}, "label": {"list"}}
_READ_ANY = {"search", "status"}
# Never run through this client, neither as a read nor as a write.
_DENIED = {"auth", "config", "alias", "extension", "secret", "variable", "gist", "ssh-key", "gpg-key"}
_CLUSTER_RE = re.compile(r"^-[A-Za-z]*[XfF]")
_NODE_RE = re.compile(r"^[A-Za-z0-9_=-]+$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9-]+$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$")
_SECRET_NAMES = (".env*", "*.env", "*.pem", "*.key", "id_*", ".npmrc", ".netrc", "auth.json", "credentials*")


class GhError(Exception):
    """A gh call failed. stderr is kept on the object, never put into the message."""

    def __init__(self, argv: Sequence[str], returncode: int, stderr: str = ""):
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"gh {' '.join(self.argv[:3])} failed (exit {returncode})")


class WriteBlocked(Exception):
    def __init__(self, decision: Decision, target: Optional[WriteTarget] = None):
        self.decision = decision
        self.target = target
        super().__init__(decision.reason)

    def to_json(self) -> Dict[str, Any]:
        """The payload the CLI prints with exit code 6."""
        repo = self.target.slug if self.target is not None and self.target.kind == "repo" else None
        return {"blocked": True, "repo": repo, "reason": self.decision.reason, "allow_cmd": self.decision.allow_cmd}


class ReadbackMismatch(Exception):
    def __init__(self, expected: Sequence[str], actual: Sequence[str]):
        self.expected = sorted(expected)
        self.actual = sorted(actual)
        super().__init__(f"label read-back differs: wanted {self.expected}, found {self.actual}")


class UnsafePath(ValueError):
    pass


# --- helpers ---------------------------------------------------------------


def _dig(obj: Any, *keys: str) -> Any:
    """obj[k1][k2]... where any missing or null step gives None."""
    for key in keys:
        obj = (obj or {}).get(key)
    return obj


def _repeat(flag: str, values: Sequence[str]) -> List[str]:
    return [part for value in values for part in (flag, value)]


def _num(value: Any) -> int:
    """A numeric id, as an int. Anything else (including a string with flags in it) is refused."""
    if isinstance(value, bool):
        raise ValueError(f"invalid number {value!r}")
    return int(value)


def _node(value: str) -> str:
    if not isinstance(value, str) or not _NODE_RE.fullmatch(value):
        raise ValueError(f"invalid node id {value!r}")
    return value


def _owner(value: str) -> str:
    if not isinstance(value, str) or not _OWNER_RE.fullmatch(value):
        raise ValueError(f"invalid owner {value!r}")
    return value


def _login(value: str) -> str:
    if not isinstance(value, str) or not _LOGIN_RE.fullmatch(value):
        raise ValueError(f"invalid login {value!r}")
    return value


def tmp_dir_name() -> str:
    """not-me-<uid>, or not-me-<user name> where there are no uids (Windows)."""
    getuid = getattr(os, "getuid", None)
    if getuid:
        return f"not-me-{getuid()}"
    try:
        user = os.environ.get("USERNAME") or os.getlogin()
    except OSError:
        user = "user"
    return "not-me-" + re.sub(r"[^A-Za-z0-9_.-]", "_", user)


def notme_tmp_dir() -> Path:
    """<runtime or temp dir>/not-me-<uid>/, private (0700) and ours. The only temp place input files may live.

    Prefers $XDG_RUNTIME_DIR when it is a directory we own, else the system temp dir. An existing path is
    never repaired: a symlink, another owner or a mode other than 0700 raises RuntimeError.
    """
    uid = getattr(os, "getuid", None)
    me = uid() if uid else None
    base = Path(tempfile.gettempdir())
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and me is not None:
        try:
            st = os.stat(runtime)
            if stat.S_ISDIR(st.st_mode) and st.st_uid == me:
                base = Path(runtime)
        except OSError:
            pass
    path = base / tmp_dir_name()
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"{path.name} in the temp directory is not a plain directory; refusing to use it")
    if me is not None and st.st_uid != me:
        raise RuntimeError(f"{path.name} in the temp directory is owned by someone else; refusing to use it")
    if me is not None and stat.S_IMODE(st.st_mode) != 0o700:
        raise RuntimeError(f"{path.name} in the temp directory is not mode 0700; refusing to use it")
    return path


# --- classification --------------------------------------------------------


def is_denied(argv: Sequence[str]) -> bool:
    """Commands this client never runs at all (credentials, config, secrets)."""
    return bool(argv) and argv[0] in _DENIED


def _api_method_ok(rest: Sequence[str]) -> bool:
    """True when an `api` argv has no method override but a single GET, and no field, input or cluster flags."""
    methods: List[str] = []
    i = 0
    while i < len(rest):
        t = rest[i]
        if t in ("-X", "--method"):
            methods.append(rest[i + 1].upper() if i + 1 < len(rest) else "")
            i += 2
            continue
        if t.startswith("--method="):
            methods.append(t[len("--method="):].upper())
        elif t.startswith("-X="):
            methods.append(t[3:].upper())
        elif t.startswith("-X") and len(t) > 2:
            methods.append(t[2:].upper())
        elif t in ("--input", "--field", "--raw-field") or t.startswith(("--input=", "--field=", "--raw-field=")):
            return False
        elif _CLUSTER_RE.match(t):
            return False  # -f/-F fields, or X/f/F hidden in a cluster such as -iXPOST
        elif "x-http-method-override" in t.lower():
            return False
        i += 1
    return methods in ([], ["GET"])


def _graphql_read_ok(rest: Sequence[str]) -> bool:
    """`graphql` followed only by `-f key=value` pairs, one inline query that holds no mutation or @file."""
    pairs = rest[1:]
    if not pairs or len(pairs) % 2 or any(pairs[i] != "-f" for i in range(0, len(pairs), 2)):
        return False
    values = pairs[1::2]
    if any("=@" in v or "mutation" in v.lower() for v in values):
        return False
    return sum(1 for v in values if v.startswith("query=")) == 1


def is_read(argv: Sequence[str]) -> bool:
    """True only for an explicitly allowed read. Everything else counts as a write (or is denied)."""
    args = list(argv)
    if not args or is_denied(args):
        return False
    if args[0] == "api":
        rest = args[1:]
        if rest and rest[0] == "graphql":
            return _graphql_read_ok(rest)
        return not any(t == "graphql" for t in rest) and _api_method_ok(rest)
    if args[0] in _READ_ANY:
        return True
    return len(args) > 1 and args[1] in _READ_VERBS.get(args[0], ())


def _default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["gh", *argv], capture_output=True, text=True)


# --- input file safety -----------------------------------------------------


def check_input_path(path: Any, project_dir: Optional[Path] = None) -> Path:
    """The resolved path if it is an allowed input file; else UnsafePath.

    Allowed: inside the project (never under .git/) or inside notme_tmp_dir(); never a secret-looking
    name, and never a file with several hard links.
    """
    resolved = Path(path).resolve()
    name = resolved.name.lower()
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in _SECRET_NAMES):
        raise UnsafePath(f"refusing to read {resolved.name!r}: it looks like a secret")
    roots = [notme_tmp_dir()] + ([Path(project_dir)] if project_dir else [])
    if not any(resolved.is_relative_to(root.resolve()) for root in roots):
        raise UnsafePath(f"input file {resolved.name!r} is outside the project directory and not-me's temp directory")
    if ".git" in resolved.parts:
        raise UnsafePath("refusing to read from inside a .git directory")
    try:
        if resolved.stat().st_nlink > 1:
            raise UnsafePath(f"refusing {resolved.name!r}: it has more than one hard link")
    except OSError:
        pass
    return resolved


# --- argv and target agreement -----------------------------------------------


def _argv_repos(argv: Sequence[str]) -> List[str]:
    """Every repo an argv names via -R/--repo/--repo= or a repos/<owner>/<name>/ path."""
    found: List[str] = []
    for i, a in enumerate(argv):
        if a in ("-R", "--repo") and i + 1 < len(argv):
            found.append(argv[i + 1])
        elif a.startswith(("--repo=", "-R=")):
            found.append(a.split("=", 1)[1])
        else:
            m = re.match(r"^/?repos/([^/?]+/[^/?]+)(?:[/?]|$)", a)
            if m:
                found.append(m.group(1))
    return found


# --- client ----------------------------------------------------------------

_PR_FIELDS = ("number,title,body,headRefName,headRefOid,baseRefName,state,isDraft,isCrossRepository,"
              "author,labels,reviewRequests,reviewDecision,mergeable")
_THREADS_QUERY = (  # {n} is an int, formatted in after validation; owner and name are -f variables
    "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
    "pullRequest(number:{n}){reviewThreads(first:100){nodes{id isResolved comments(first:100){nodes{"
    "id databaseId author{login} authorAssociation body path line url}}}}}}}"
)
_THREAD_REPO_QUERY = ("query($id:ID!){node(id:$id){... on PullRequestReviewThread{repository{nameWithOwner}}}}")
_RESOLVE_MUTATION = ("mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id isResolved}}}")
_BOARD_QUERY = (
    "query($owner:String!){repositoryOwner(login:$owner){... on ProjectV2Owner{projectV2(number:{n}){"
    "id field(name:\"Status\"){... on ProjectV2SingleSelectField{id options{id name}}}"
    "items(first:100){nodes{id fieldValueByName(name:\"Status\"){... on ProjectV2ItemFieldSingleSelectValue{name}}"
    "content{... on Issue{number repository{nameWithOwner}} ... on PullRequest{number repository{nameWithOwner}}}}}}}}}"
)
_BOARD_PROJECT_ID_QUERY = (
    "query($owner:String!){repositoryOwner(login:$owner){... on ProjectV2Owner{projectV2(number:{n}){id}}}}"
)
_BOARD_SET_MUTATION = (
    "mutation($project:ID!,$item:ID!,$field:ID!,$option:String!){updateProjectV2ItemFieldValue(input:{projectId:$project,"
    "itemId:$item,fieldId:$field,value:{singleSelectOptionId:$option}}){projectV2Item{id}}}"
)
_REVIEW_EVENTS = ("COMMENT", "REQUEST_CHANGES")


class GhClient:
    def __init__(self, ctx: GuardContext, runner: Optional[Runner] = None, dry_run: Optional[bool] = None,
                 ledger: Optional[Ledger] = None):
        self.ctx = ctx
        self.runner: Runner = runner or _default_runner
        self.dry_run = ctx.dry_run if dry_run is None else dry_run
        self._ledger = ledger

    @property
    def ledger(self) -> Optional[Ledger]:
        if self._ledger is None and self.ctx.project_dir is not None:
            self._ledger = Ledger.default_for(self.ctx.project_dir)
        return self._ledger

    # --- core ---

    def _run(self, argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        try:
            proc = self.runner(list(argv))
        except OSError as exc:
            raise GhError(argv, 127, exc.__class__.__name__)
        if proc.returncode != 0:
            raise GhError(argv, proc.returncode, proc.stderr or "")
        return proc

    def read(self, argv: Sequence[str], json: bool = True) -> Any:
        if not is_read(argv):
            raise GhError(argv, 0, "refusing to run a write argv through read(); use write()")
        out = self._run(argv).stdout
        if not json:
            return out
        if not out.strip():
            return None
        try:
            return _json.loads(out)
        except ValueError:
            raise GhError(argv, 0, "response was not valid JSON")

    def _gate(self, target: WriteTarget) -> None:
        decision = writes.check_write(target, self.ctx)
        if not decision.ok:
            raise WriteBlocked(decision, target)

    def write(self, target: WriteTarget, argv: Sequence[str], *, payload_path: Any = None, json_input: Any = None,
              description: str = "", post: Optional[Callable[[Any], Any]] = None) -> Any:
        """Gate, then dry-run to the ledger or run. post(result) runs only on a real result.

        payload_path: a body file shown in the ledger. json_input: a JSON body sent with --input; its
        temp file is made only when the call really runs. ValueError if argv names a different repo than
        target, or is a command this client never runs.
        """
        argv = list(argv)
        if is_denied(argv):
            raise ValueError(f"gh {argv[0]} is not available through not-me")
        if target.kind == "repo":
            others = [r for r in _argv_repos(argv) if r.lower() != target.slug.lower()]
            if others:
                raise ValueError(f"argv names {others[0]!r} but the write target is {target.slug!r}")
        if payload_path is not None:
            payload_path = check_input_path(payload_path, self.ctx.project_dir)
        self._gate(target)
        payload = _json.dumps(json_input).encode("utf-8") if json_input is not None else None
        if self.dry_run:
            if json_input is not None:
                argv += ["--input", "<json payload>"]
            if self.ledger is not None:
                self.ledger.append("gh " + " ".join(argv[:2]), target.describe(), argv, payload_path, description,
                                   payload_bytes=payload)
            return {"dry_run": True, "would": argv}
        tmp: Optional[Path] = None
        if payload is not None:
            fd, name = tempfile.mkstemp(prefix="not-me-", suffix=".json", dir=str(notme_tmp_dir()))
            tmp = Path(name)
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
            argv += ["--input", name]
        try:
            out = self._run(argv).stdout.strip()
        finally:
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        result: Any = {"output": out}
        if out[:1] in ("{", "["):
            try:
                result = _json.loads(out)
            except ValueError:
                pass
        return post(result) if post else result

    def _repo(self, repo: str) -> WriteTarget:
        if not writes.valid_slug(repo):
            raise ValueError(f"invalid repo {repo!r}")
        return WriteTarget.repo(repo)

    def _write_body(self, repo: str, argv: Sequence[str], body_index: int, body_file: Any, description: str,
                    post: Optional[Callable[[Any], Any]] = None) -> Any:
        """A repo write; the body file's path replaces argv[body_index] (or the text after its last '@')."""
        path = str(check_input_path(body_file, self.ctx.project_dir))
        argv = list(argv)
        at = argv[body_index].rfind("@")
        argv[body_index] = argv[body_index][:at + 1] + path if at >= 0 else path
        return self.write(self._repo(repo), argv, payload_path=path, description=description, post=post)

    def _api_body(self, repo: str, method: str, endpoint: str, body_file: Any, description: str) -> Any:
        return self._write_body(repo, ["api", "-X", method, endpoint, "-F", "body=@"], -1, body_file, description)

    def _graphql(self, query: str, **variables: str) -> List[str]:
        """argv for a graphql call; variables are plain -f strings (numbers are formatted into the query)."""
        return ["api", "graphql", "-f", f"query={query}"] + _repeat("-f", [f"{k}={v}" for k, v in variables.items()])

    # --- reads ---

    def me(self) -> str:
        return self.read(["api", "user"])["login"]

    def repo_default_branch(self, repo: str) -> Optional[str]:
        self._repo(repo)
        return _dig(self.read(["repo", "view", repo, "--json", "defaultBranchRef"]), "defaultBranchRef", "name")

    def issue(self, repo: str, n: int) -> Dict[str, Any]:
        self._repo(repo)
        return self.read(["issue", "view", str(_num(n)), "-R", repo, "--json",
                          "number,title,body,state,labels,assignees,author,comments,url"])

    def labels(self, repo: str, n: int) -> List[str]:
        self._repo(repo)
        data = self.read(["api", f"repos/{repo}/issues/{_num(n)}/labels?per_page=100"]) or []
        return [item["name"] for item in data]

    def comments(self, repo: str, n: int) -> List[Dict[str, Any]]:
        self._repo(repo)
        data = self.read(["api", f"repos/{repo}/issues/{_num(n)}/comments?per_page=100"]) or []
        return [{"id": c["id"], "author": _dig(c, "user", "login"),
                 "association": c.get("author_association"), "body": c.get("body", ""),
                 "created_at": c.get("created_at"), "updated_at": c.get("updated_at"), "url": c.get("html_url")}
                for c in data]

    def pr(self, repo: str, n: int) -> Dict[str, Any]:
        self._repo(repo)
        return self.read(["pr", "view", str(_num(n)), "-R", repo, "--json", _PR_FIELDS])

    def pr_list(self, repo: str, label: Optional[str] = None, assignee: Optional[str] = None,
                state: str = "open") -> List[Dict[str, Any]]:
        self._repo(repo)
        argv = ["pr", "list", "-R", repo, "--state", state, "--json",
                "number,title,headRefName,baseRefName,isDraft,author,labels,assignees,url"]
        argv += _repeat("--label", [label] if label else []) + _repeat("--assignee", [assignee] if assignee else [])
        return self.read(argv) or []

    def review_threads(self, repo: str, n: int) -> List[Dict[str, Any]]:
        owner, _, name = self._repo(repo).slug.partition("/")
        query = _THREADS_QUERY.replace("{n}", str(_num(n)))
        data = self.read(self._graphql(query, owner=owner, name=name))
        nodes = _dig(data, "data", "repository", "pullRequest", "reviewThreads", "nodes") or []
        return [{"id": t.get("id"), "isResolved": bool(t.get("isResolved")),
                 "comments": [{**c, "author": _dig(c, "author", "login")}
                              for c in _dig(t, "comments", "nodes") or []]} for t in nodes]

    def _board_project(self, owner: str, number: int, query: str) -> Dict[str, Any]:
        data = self.read(self._graphql(query.replace("{n}", str(_num(number))), owner=_owner(owner)))
        return _dig(data, "data", "repositoryOwner", "projectV2") or {}

    def board_item_status(self, owner: str, number: int, item_id: Optional[str] = None) -> Dict[str, Any]:
        project = self._board_project(owner, number, _BOARD_QUERY)
        items = [{"id": n.get("id"), "number": _dig(n, "content", "number"),
                  "repo": _dig(n, "content", "repository", "nameWithOwner"),
                  "status": _dig(n, "fieldValueByName", "name")} for n in _dig(project, "items", "nodes") or []]
        if item_id is not None:
            items = [i for i in items if i["id"] == item_id]
        field = project.get("field") or {}
        return {"project_id": project.get("id"),
                "status_field": {"id": field.get("id"), "options": field.get("options") or []}, "items": items}

    # --- writes ---

    def labels_set(self, repo: str, n: int, names: Sequence[str]) -> Any:
        """Replace the full label set with one PUT, then read it back. Never add+remove in one edit."""
        n = _num(n)

        def readback(_: Any) -> List[str]:
            actual = self.labels(repo, n)
            if set(actual) != set(names):
                raise ReadbackMismatch(names, actual)
            return actual

        return self.write(self._repo(repo), ["api", "-X", "PUT", f"repos/{repo}/issues/{n}/labels"],
                          json_input={"labels": list(names)}, description=f"set labels on {repo}#{n}",
                          post=readback)

    def comment_create(self, repo: str, n: int, body_file: Any) -> Any:
        n = _num(n)
        return self._write_body(repo, ["issue", "comment", str(n), "-R", repo, "--body-file", ""], -1, body_file,
                                f"comment on {repo}#{n}")

    def comment_update(self, repo: str, comment_id: int, body_file: Any) -> Any:
        comment_id = _num(comment_id)
        return self._api_body(repo, "PATCH", f"repos/{repo}/issues/comments/{comment_id}", body_file,
                              f"edit comment {comment_id} on {repo}")

    def reply_thread(self, repo: str, n: int, comment_id: int, body_file: Any) -> Any:
        n, comment_id = _num(n), _num(comment_id)
        return self._api_body(repo, "POST", f"repos/{repo}/pulls/{n}/comments/{comment_id}/replies", body_file,
                              f"reply on {repo}#{n}")

    def pr_edit_body(self, repo: str, n: int, body_file: Any) -> Any:
        n = _num(n)
        return self._write_body(repo, ["pr", "edit", str(n), "-R", repo, "--body-file", ""], -1, body_file,
                                f"edit PR body {repo}#{n}")

    def review_post(self, repo: str, n: int, event: str, body_file: Any, comments_json: Any = None,
                    allow_approve: bool = False) -> Any:
        """Post a review. APPROVE is refused unless the caller passes allow_approve=True."""
        n = _num(n)
        if event not in _REVIEW_EVENTS + (("APPROVE",) if allow_approve else ()):
            raise ValueError(f"review event {event!r} is not allowed here")
        root = self.ctx.project_dir
        data: Dict[str, Any] = {"event": event,
                                "body": check_input_path(body_file, root).read_text(encoding="utf-8")}
        if comments_json is not None:
            data["comments"] = _json.loads(check_input_path(comments_json, root).read_text(encoding="utf-8"))
        return self.write(self._repo(repo), ["api", "-X", "POST", f"repos/{repo}/pulls/{n}/reviews"],
                          json_input=data, description=f"review ({event}) on {repo}#{n}")

    def request_review(self, repo: str, n: int, logins: Sequence[str]) -> Any:
        n = _num(n)
        argv = ["api", "-X", "POST", f"repos/{repo}/pulls/{n}/requested_reviewers"]
        argv += _repeat("-f", [f"reviewers[]={_login(login)}" for login in logins])
        return self.write(self._repo(repo), argv, description=f"request review on {repo}#{n}")

    def pr_create(self, repo: str, head: str, base: str, title: str, body_file: Any,
                  labels: Sequence[str] = (), reviewers: Sequence[str] = (), assignee: Optional[str] = None,
                  draft: bool = False) -> Dict[str, Any]:
        def parse(result: Any) -> Dict[str, Any]:
            out = result.get("output", "")
            m = re.search(r"/pull/(\d+)", out)
            return {"url": out.splitlines()[-1] if out else "", "number": int(m.group(1)) if m else None}

        argv = ["pr", "create", "-R", repo, "--head", head, "--base", base, "--title", title, "--body-file", ""]
        body_index = len(argv) - 1
        argv += _repeat("--label", labels) + _repeat("--reviewer", [_login(r) for r in reviewers])
        argv += _repeat("--assignee", [_login(assignee)] if assignee else []) + (["--draft"] if draft else [])
        return self._write_body(repo, argv, body_index, body_file, f"open PR {head} -> {base}", post=parse)

    def resolve_thread(self, thread_id: str) -> Any:
        """Resolve a review thread. The write is gated on the repository the thread belongs to."""
        thread_id = _node(thread_id)
        data = self.read(self._graphql(_THREAD_REPO_QUERY, id=thread_id))
        slug = _dig(data, "data", "node", "repository", "nameWithOwner")
        if not slug:
            raise GhError(["api", "graphql"], 0, "thread not found")
        return self.write(self._repo(slug), self._graphql(_RESOLVE_MUTATION, id=thread_id),
                          description="resolve review thread")

    def board_set_status(self, owner: str, number: int, item_id: str, field_id: str, option_id: str) -> Any:
        target = WriteTarget.board(_owner(owner), _num(number))
        item_id, field_id, option_id = _node(item_id), _node(field_id), _node(option_id)
        self._gate(target)  # fail closed before any lookup if this is not the configured board
        project_id = self._board_project(owner, number, _BOARD_PROJECT_ID_QUERY).get("id")
        if not project_id:
            raise GhError(["api", "graphql"], 0, "board not found")
        argv = self._graphql(_BOARD_SET_MUTATION, project=_node(project_id), item=item_id,
                             field=field_id, option=option_id)
        return self.write(target, argv, description=f"set board status on {owner}/{number}")
