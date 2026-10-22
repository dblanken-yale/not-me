"""Readers: answer "what is the state now" from live GitHub (gh) or from the dry-run ledger.

Both expose the same methods. A method returns None when the backend cannot observe the thing
(for example commits from a ledger); assertions turn that into a skipped result.

PR numbers: an int for PRs that existed at seeding, or the string "new:K" (K from 1) for the K-th PR
the run opened in a repo. On GitHub "new:K" is the K-th PR whose number is not in the manifest and is above
the manifest's PR floor (the highest PR number at seeding, so PRs closed by earlier runs never count as new).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional, Sequence

LOCK_TITLE = "eval-lock"


class ReaderError(RuntimeError):
    def __init__(self, message: str, not_found: bool = False):
        super().__init__(message)
        self.not_found = not_found


def default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    """Run `gh <argv>`; argv does not include the gh word (the fakegh runner takes the same shape)."""
    return subprocess.run(["gh", *argv], capture_output=True, text=True, encoding="utf-8")


def is_new(number: Any) -> bool:
    return isinstance(number, str) and number.startswith("new:")


def names(items: Any, key: str = "name") -> List[str]:
    return sorted(i[key] if isinstance(i, dict) else i for i in (items or []))


def _count(value: Any) -> int:
    return len(value) if isinstance(value, list) else int(value or 0)


def diff_sections(expected: Dict[str, Any], actual: Dict[str, Any]) -> List[str]:
    """Differences between two snapshots, over branches, issues and prs only."""
    out: List[str] = []
    for section in ("branches", "issues", "prs"):
        exp, act = expected.get(section) or {}, actual.get(section) or {}
        for key in sorted(set(exp) | set(act)):
            if key not in act:
                out.append(f"missing {section}/{key}")
            elif key not in exp:
                out.append(f"unexpected {section}/{key}")
            elif exp[key] != act[key]:
                out.append(f"changed {section}/{key}: expected {json.dumps(exp[key], sort_keys=True)}, "
                           f"got {json.dumps(act[key], sort_keys=True)}")
    return out


class GhReader:
    kind = "github"

    def __init__(self, runner: Optional[Callable[[Sequence[str]], Any]] = None, manifest: Optional[dict] = None):
        self.runner = runner or default_runner
        self.manifest = manifest or {}

    # -- plumbing
    def _json(self, argv: Sequence[str]) -> Any:
        proc = self.runner(list(argv))
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            raise ReaderError(f"gh {' '.join(argv[:3])} failed: {err}",
                              not_found=("404" in err or "Not Found" in err))
        text = (proc.stdout or "").strip()
        return json.loads(text) if text else None

    def _api(self, endpoint: str) -> Any:
        return self._json(["api", endpoint])

    def _seeded_numbers(self, repo: str) -> set:
        m = self.manifest
        created = m.get("created") if repo == m.get("repo") else m.get("companion_created")
        snap = m.get("snapshot") if repo == m.get("repo") else m.get("companion_snapshot")
        nums = {p["number"] for p in (created or {}).get("prs", [])}
        nums |= {int(n) for n in ((snap or {}).get("prs") or {})}
        return nums

    def _pr_floor(self, repo: str) -> int:
        m = self.manifest
        return int(m.get("pr_floor" if repo == m.get("repo") else "companion_pr_floor") or 0)

    # -- pull requests
    def _all_prs(self, repo: str) -> List[dict]:
        rows = self._json(["pr", "list", "-R", repo, "--state", "all", "--limit", "200", "--json",
                           "number,title,body,state,headRefName,baseRefName,headRefOid,labels,createdAt"]) or []
        return [{"repo": repo, "number": r["number"], "title": r["title"], "body": r.get("body") or "",
                 "state": r["state"], "head": r["headRefName"], "base": r["baseRefName"],
                 "head_sha": r.get("headRefOid", ""), "labels": names(r.get("labels")),
                 "created_at": r.get("createdAt", "")} for r in rows]

    def new_prs(self, repo: str) -> List[dict]:
        seeded, floor = self._seeded_numbers(repo), self._pr_floor(repo)
        fresh = sorted((p for p in self._all_prs(repo) if p["number"] not in seeded and p["number"] > floor),
                       key=lambda p: p["number"])
        for k, pr in enumerate(fresh, 1):
            pr["ref_number"] = pr["number"]
            pr["number"] = f"new:{k}"
        return fresh

    def pr(self, repo: str, number: Any) -> Optional[dict]:
        number = self.real_number(repo, number)
        for p in self._all_prs(repo):
            if p["number"] == number:
                return p
        return None

    def real_number(self, repo: str, number: Any) -> int:
        if not is_new(number):
            return int(number)
        fresh = self.new_prs(repo)
        k = int(number.split(":")[1])
        if k > len(fresh):
            raise ReaderError(f"no PR {number} in {repo}")
        return fresh[k - 1]["ref_number"]

    def pr_delta(self, repo: str, number: Any, since_sha: Optional[str] = None) -> Optional[dict]:
        """Commit subjects and changed paths: since `since_sha` when given, else the whole PR."""
        if since_sha:
            pr = self.pr(repo, number)
            head = pr["head_sha"] if pr else ""
            data = self._api(f"repos/{repo}/compare/{since_sha}...{head}")
            return {"commits": [c["commit"]["message"] for c in data.get("commits", [])],
                    "files": [f["filename"] for f in data.get("files", [])]}
        n = self.real_number(repo, number)
        commits = self._api(f"repos/{repo}/pulls/{n}/commits?per_page=100") or []
        files = self._api(f"repos/{repo}/pulls/{n}/files?per_page=100") or []
        return {"commits": [c["commit"]["message"] for c in commits], "files": [f["filename"] for f in files]}

    # -- refs
    def branches(self, repo: str) -> Dict[str, str]:
        rows = self._api(f"repos/{repo}/branches?per_page=100") or []
        return {r["name"]: r["commit"]["sha"] for r in rows}

    def branch_sha(self, repo: str, name: str) -> Optional[str]:
        try:
            return self._api(f"repos/{repo}/branches/{name}")["commit"]["sha"]
        except ReaderError as exc:
            if exc.not_found:
                return None
            raise

    def compare_status(self, repo: str, base: str, head: str) -> Optional[str]:
        try:
            return self._api(f"repos/{repo}/compare/{base}...{head}")["status"]
        except ReaderError as exc:
            if exc.not_found:
                return "missing"
            raise

    # -- labels, comments, reviews, threads
    def labels(self, repo: str, number: Any) -> List[str]:
        return names(self._api(f"repos/{repo}/issues/{self.real_number(repo, number)}/labels"))

    def comments(self, repo: str, number: Any) -> List[str]:
        rows = self._api(f"repos/{repo}/issues/{self.real_number(repo, number)}/comments?per_page=100") or []
        return [r.get("body") or "" for r in rows]

    def reviews(self, repo: str, number: Any) -> List[dict]:
        rows = self._api(f"repos/{repo}/pulls/{self.real_number(repo, number)}/reviews") or []
        return [{"event": {"APPROVED": "APPROVE", "CHANGES_REQUESTED": "REQUEST_CHANGES",
                           "COMMENTED": "COMMENT"}.get(r["state"], r["state"]), "body": r.get("body") or ""}
                for r in rows]

    def review_comments(self, repo: str, number: Any) -> List[dict]:
        rows = self._api(f"repos/{repo}/pulls/{self.real_number(repo, number)}/comments?per_page=100") or []
        return [{"path": r.get("path"), "line": r.get("line") or r.get("original_line"), "body": r.get("body") or ""}
                for r in rows]

    def threads(self, repo: str, number: Any) -> List[dict]:
        owner, name = repo.split("/", 1)
        n = self.real_number(repo, number)
        query = ("query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
                 f"pullRequest(number:{n}){{reviewThreads(first:100){{nodes{{id isResolved "
                 "comments(first:50){nodes{databaseId author{login} body path line}}}}}}}}}")
        data = self._json(["api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}"])
        nodes = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        return [{"id": t["id"], "resolved": bool(t["isResolved"]), "replies": max(len(t["comments"]["nodes"]) - 1, 0),
                 "first_body": (t["comments"]["nodes"] or [{}])[0].get("body", "")} for t in nodes]

    # -- whole-repo views
    def snapshot(self, repo: str) -> dict:
        issues = self._json(["issue", "list", "-R", repo, "--state", "all", "--limit", "1000", "--json",
                             "number,title,state,labels,assignees,comments"]) or []
        prs = self._json(["pr", "list", "-R", repo, "--state", "open", "--limit", "1000", "--json",
                          "number,title,state,headRefName,baseRefName,headRefOid,labels,assignees,comments,reviews"]) or []
        return {
            "branches": self.branches(repo),
            "issues": {str(i["number"]): {"title": i["title"], "state": i["state"], "labels": names(i.get("labels")),
                                          "assignees": names(i.get("assignees"), "login"),
                                          "comments": _count(i.get("comments"))}
                       for i in issues if i["title"] != LOCK_TITLE},
            "prs": {str(p["number"]): {"title": p["title"], "state": p["state"], "head": p["headRefName"],
                                       "base": p["baseRefName"], "head_sha": p.get("headRefOid", ""),
                                       "labels": names(p.get("labels")),
                                       "assignees": names(p.get("assignees"), "login"),
                                       "comments": _count(p.get("comments")), "reviews": _count(p.get("reviews"))}
                    for p in prs},
        }

    def pr_floor(self, repo: str) -> int:
        """The highest PR number in `repo`, open or closed (0 when it has none)."""
        rows = self._json(["pr", "list", "-R", repo, "--state", "all", "--limit", "1000", "--json", "number"]) or []
        return max((r["number"] for r in rows), default=0)

    def snapshot_diff(self, repo: str, expected: dict) -> Optional[List[str]]:
        return diff_sections(expected, self.snapshot(repo))

    def written_text(self, refs: Sequence[tuple]) -> List[str]:
        """Text the run could have written: new PR titles and bodies, commit subjects, and comments on `refs`."""
        out: List[str] = []
        repos = {r for r, _ in refs}
        for repo in sorted(repos):
            for pr in self.new_prs(repo):
                out += [pr["title"], pr["body"]]
                delta = self.pr_delta(repo, pr["number"])
                out += (delta or {}).get("commits", [])
                out += self.comments(repo, pr["number"])
        for repo, number in refs:
            if not is_new(number):
                out += self.comments(repo, number)
        return out

    def writes(self) -> Optional[List[dict]]:
        return None


# ---------------------------------------------------------------------------------------------


def _flag_values(argv: Sequence[str], flag: str) -> List[str]:
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == flag]


def _repo_of(argv: Sequence[str]) -> Optional[str]:
    for v in _flag_values(argv, "-R") + _flag_values(argv, "--repo"):
        return v
    for a in argv:
        m = re.match(r"repos/([^/]+/[^/]+)/", a)
        if m:
            return m.group(1)
    return None


class LedgerReader:
    """Answers from the dry-run ledger (JSONL of intended writes) plus the manifest's seeded state."""

    kind = "ledger"

    def __init__(self, ledger_path: Any, manifest: Optional[dict] = None):
        self.path = str(ledger_path)
        self.manifest = manifest or {}
        self.dir = os.path.dirname(self.path) or "."
        self._writes: Optional[List[dict]] = None

    def _payload(self, entry: dict) -> str:
        rel = entry.get("payload_path")
        if not rel:
            return ""
        try:
            with open(os.path.join(self.dir, rel), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    def writes(self) -> List[dict]:
        if self._writes is not None:
            return self._writes
        entries: List[dict] = []
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        try:
                            entries.append(json.loads(line))
                        except ValueError:
                            continue
        out = []
        news: Dict[str, int] = {}
        for index, e in enumerate(entries):
            argv = [str(a) for a in e.get("argv", [])]
            w = {"index": index, "argv": argv, "repo": _repo_of(argv), "kind": "other", "number": None,
                 "text": self._payload(e), "target": e.get("target", ""), "op": e.get("op", "")}
            joined = " ".join(argv)
            ep = next((a for a in argv if a.startswith("repos/")), "")
            m_num = re.match(r"repos/[^/]+/[^/]+/(?:issues|pulls)/(\d+|<[^>]+>)(/.*)?$", ep)
            if argv[:2] == ["pr", "create"]:
                repo = w["repo"] or ""
                news[repo] = news.get(repo, 0) + 1
                w.update(kind="pr_create", number=f"new:{news[repo]}", head=(_flag_values(argv, "--head") or [""])[0],
                         base=(_flag_values(argv, "--base") or [""])[0],
                         title=(_flag_values(argv, "--title") or [""])[0], labels=_flag_values(argv, "--label"))
            elif argv[:2] == ["pr", "merge"]:
                w["kind"] = "merge"
            elif argv[:2] in (["issue", "comment"], ["pr", "comment"]) and len(argv) > 2:
                w.update(kind="comment", number=int(argv[2]) if argv[2].isdigit() else None)
            elif argv[:2] in (["pr", "edit"], ["issue", "edit"]) and len(argv) > 2:
                w.update(kind="edit", number=int(argv[2]) if argv[2].isdigit() else None)
            elif "graphql" in argv and "resolveReviewThread" in joined:
                w["kind"] = "resolve_thread"
            elif m_num and ep.endswith("/labels") and "PUT" in argv:
                w.update(kind="labels", number=self._num(m_num.group(1), w["repo"] or "", news),
                         labels=sorted(self._labels_payload(w["text"])))
            elif m_num and re.search(r"/pulls/\d+/reviews$", ep):
                data = self._json_text(w["text"])
                w.update(kind="review", number=int(m_num.group(1)), event=data.get("event"), text=data.get("body", ""),
                         comments=data.get("comments") or [])
            elif m_num and re.search(r"/comments/\d+/replies$", ep):
                w.update(kind="thread_reply", number=int(m_num.group(1)))
            elif m_num and ep.endswith("/issues/%s/comments" % m_num.group(1)):
                w.update(kind="comment", number=int(m_num.group(1)) if m_num.group(1).isdigit() else None)
            elif re.search(r"/issues/comments/\d+$", ep):
                w["kind"] = "comment_edit"
            out.append(w)
        self._writes = out
        return out

    @staticmethod
    def _json_text(text: str) -> dict:
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def _labels_payload(self, text: str) -> List[str]:
        return list(self._json_text(text).get("labels") or [])

    @staticmethod
    def _num(token: str, repo: str, news: Dict[str, int]) -> Any:
        if token.isdigit():
            return int(token)
        return f"new:{news.get(repo, 1)}"  # "<new PR>" placeholder: the latest PR opened in that repo

    # -- the Reader interface
    def new_prs(self, repo: str) -> List[dict]:
        out = []
        for w in self.writes():
            if w["kind"] == "pr_create" and w["repo"] == repo:
                out.append({"repo": repo, "number": w["number"], "title": w["title"], "body": w["text"],
                            "state": "OPEN", "head": w["head"], "base": w["base"], "head_sha": "",
                            "labels": self.labels(repo, w["number"]), "created_at": w["index"]})
        return out

    def _seeded(self, repo: str, section: str, number: int) -> Optional[dict]:
        m = self.manifest
        snap = m.get("snapshot") if repo == m.get("repo") else m.get("companion_snapshot")
        return ((snap or {}).get(section) or {}).get(str(number))

    def pr(self, repo: str, number: Any) -> Optional[dict]:
        if is_new(number):
            return next((p for p in self.new_prs(repo) if p["number"] == number), None)
        row = self._seeded(repo, "prs", number)
        if row is None:
            return None
        merged = any(w["kind"] == "merge" and w["repo"] == repo and w["number"] == number for w in self.writes())
        return {"repo": repo, "number": number, "state": "MERGED" if merged else row.get("state", "OPEN"),
                "head": row.get("head"), "base": row.get("base"), "head_sha": row.get("head_sha", ""),
                "title": row.get("title", ""), "body": "", "labels": self.labels(repo, number)}

    def pr_delta(self, repo: str, number: Any, since_sha: Optional[str] = None) -> Optional[dict]:
        return None

    def branches(self, repo: str) -> Optional[Dict[str, str]]:
        return None

    def branch_sha(self, repo: str, name: str) -> Optional[str]:
        return None

    def compare_status(self, repo: str, base: str, head: str) -> Optional[str]:
        return None

    def labels(self, repo: str, number: Any) -> List[str]:
        if is_new(number):
            created = [w for w in self.writes() if w["kind"] == "pr_create" and w["repo"] == repo
                       and w["number"] == number]
            current = sorted(created[0]["labels"]) if created else []
        else:
            row = self._seeded(repo, "issues", number) or self._seeded(repo, "prs", number) or {}
            current = sorted(row.get("labels", []))
        for w in self.writes():
            if w["kind"] == "labels" and w["repo"] == repo and w["number"] == number:
                current = w["labels"]
        return current

    def comments(self, repo: str, number: Any) -> List[str]:
        return [w["text"] for w in self.writes()
                if w["kind"] == "comment" and w["repo"] == repo and w["number"] == number]

    def reviews(self, repo: str, number: Any) -> List[dict]:
        return [{"event": w["event"], "body": w["text"]} for w in self.writes()
                if w["kind"] == "review" and w["repo"] == repo and w["number"] == number]

    def review_comments(self, repo: str, number: Any) -> List[dict]:
        return [{"path": c.get("path"), "line": c.get("line"), "body": c.get("body", "")}
                for w in self.writes() if w["kind"] == "review" and w["repo"] == repo and w["number"] == number
                for c in w["comments"]]

    def threads(self, repo: str, number: Any) -> Optional[List[dict]]:
        return None

    def thread_actions(self, repo: str, number: Any) -> dict:
        ws = self.writes()
        return {"replies": sum(1 for w in ws if w["kind"] == "thread_reply" and w["repo"] == repo
                               and w["number"] == number),
                "resolved": any(w["kind"] == "resolve_thread" for w in ws)}

    def snapshot_diff(self, repo: str, expected: dict) -> List[str]:
        return [f"intended write: {' '.join(w['argv'][:4])}" for w in self.writes() if w["repo"] in (repo, None)]

    def written_text(self, refs: Sequence[tuple]) -> List[str]:
        out = []
        for w in self.writes():
            out += [w["text"], w.get("title", ""), " ".join(w["argv"])]
        return out
