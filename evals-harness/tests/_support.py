"""Shared test setup: import paths, a fakegh extended with the endpoints the graders read, and a World
that builds a seeded fixture state plus the manifest that describes it."""
import copy
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent
ROOT = HARNESS.parent
PLUGIN = ROOT / "plugins" / "not-me"
for p in (str(PLUGIN), str(HARNESS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from tests.fakes.fakegh import FakeRunner, seed_state  # noqa: E402
import readers  # noqa: E402

MAIN = "acme/app"
COMP = "acme/app-ui"
SCENARIOS = ROOT / "fixture" / "scenarios"


def scenario_expect(sid):
    return json.loads((SCENARIOS / f"{sid}.json").read_text(encoding="utf-8"))["expect"]


class HarnessFake(FakeRunner):
    """fakegh plus branches, compare, PR commits and files, PR inline comments, and a reviews field."""

    def __init__(self, state=None):
        super().__init__(state)
        self.history = {}  # repo -> branch -> [sha]
        self.msgs = {}  # sha -> message
        self.files = {}  # sha -> [paths]
        self._n = 0

    @staticmethod
    def _view(thing, fields):
        extra = {}
        if "reviews" in fields:
            extra["reviews"] = list(thing.get("reviews", []))
        base = FakeRunner._view(thing, [f for f in fields if f != "reviews"])
        base.update(extra)
        return base

    def _routes(self):
        r = r"repos/([^/]+/[^/]+)"
        return super()._routes() + [
            ("GET", r + r"/branches", self._r_branches),
            ("GET", r + r"/branches/(.+)", self._r_branch),
            ("GET", r + r"/compare/(.+)\.\.\.(.+)", self._r_compare),
            ("GET", r + r"/pulls/(\d+)/commits", self._r_pr_commits),
            ("GET", r + r"/pulls/(\d+)/files", self._r_pr_files),
            ("GET", r + r"/pulls/(\d+)/comments", self._r_pr_comments),
        ]

    # -- git model
    def new_sha(self, message, files):
        self._n += 1
        sha = f"{self._n:040x}"
        self.msgs[sha] = message
        self.files[sha] = list(files)
        return sha

    def _hist(self, repo, ref):
        branches = self.history.get(repo, {})
        if ref in branches:
            return branches[ref]
        for hist in branches.values():
            if ref in hist:
                return hist[:hist.index(ref) + 1]
        raise KeyError(f"HTTP 404: Not Found ({ref})")

    def _r_branches(self, g, flags):
        return [{"name": b, "commit": {"sha": h[-1]}} for b, h in self.history.get(g[0], {}).items()]

    def _r_branch(self, g, flags):
        hist = self.history.get(g[0], {}).get(g[1])
        if hist is None:
            raise KeyError("HTTP 404: Not Found (branch)")
        return {"name": g[1], "commit": {"sha": hist[-1]}}

    def _r_compare(self, g, flags):
        repo, a, b = g
        hist = self._hist(repo, b)
        if a not in self.msgs:
            raise KeyError("HTTP 404: Not Found (base)")
        if a == hist[-1]:
            return {"status": "identical", "commits": [], "files": []}
        if a in hist:
            new = hist[hist.index(a) + 1:]
            return {"status": "ahead",
                    "commits": [{"sha": s, "commit": {"message": self.msgs[s]}} for s in new],
                    "files": [{"filename": f} for s in new for f in self.files[s]]}
        return {"status": "diverged", "commits": [], "files": []}

    def _pr_shas(self, repo, n):
        thing = self._thing(repo, n)
        return thing["history_fork"], self._hist(repo, thing["headRefName"])

    def _r_pr_commits(self, g, flags):
        fork, hist = self._pr_shas(g[0], g[1])
        return [{"sha": s, "commit": {"message": self.msgs[s]}} for s in hist[fork:]]

    def _r_pr_files(self, g, flags):
        fork, hist = self._pr_shas(g[0], g[1])
        return [{"filename": f} for s in hist[fork:] for f in self.files[s]]

    def _r_pr_comments(self, g, flags):
        return list(self._thing(*g).get("inline", []))


class World:
    def __init__(self):
        self.gh = HarnessFake()
        for slug in (MAIN, COMP):
            repo = self.gh.state["repos"][slug]
            repo["things"] = {}
            repo["next_number"] = 1
        self.gh.state["timeline"] = []
        for slug in (MAIN, COMP):
            sha = self.gh.new_sha("init", ["README.md"])
            self.gh.history[slug] = {"main": [sha], "develop": [sha]}
        self.issues, self.prs = {}, {}

    # -- building state
    def _next(self, repo):
        data = self.gh.state["repos"][repo]
        n = data["next_number"]
        data["next_number"] += 1
        return n

    def issue(self, key, labels=(), comments=0, repo=MAIN, author="octocat", title=None):
        n = self._next(repo)
        self.gh.state["repos"][repo]["things"][str(n)] = {
            "number": n, "title": title or f"issue {key}", "body": f"body {key}", "state": "OPEN",
            "labels": list(labels), "assignees": [], "author": author,
            "comments": [{"id": 9000 + i, "user": "octocat", "assoc": "OWNER", "body": "hello",
                          "created_at": "2026-01-01T00:00:00Z"} for i in range(comments)], "kind": "issue"}
        self.issues[(repo, key)] = n
        return n

    def push(self, repo, branch, message, files):
        sha = self.gh.new_sha(message, files)
        self.gh.history[repo][branch].append(sha)
        for thing in self.gh.state["repos"][repo]["things"].values():
            if thing["kind"] == "pr" and thing["headRefName"] == branch:
                thing["headRefOid"] = sha
        return sha

    def pr(self, key, head, labels=(), base="main", title="a pr", body="", commits=(), files=("calc/core.py",),
           repo=MAIN, threads=(), seeded=True, author="octocat"):
        hist = self.gh.history[repo]
        if head not in hist:
            hist[head] = list(hist[base])
        fork = len(hist[head])
        for msg in commits or ["seed commit"]:
            hist[head].append(self.gh.new_sha(msg, list(files)))
        n = self._next(repo)
        thread_rows = [{"id": f"PRRT_{n}_{i}", "isResolved": False, "comments": [
            {"id": f"PRRC_{n}_{i}", "databaseId": 700 + i, "author": who, "authorAssociation": "MEMBER",
             "body": body_i, "path": "calc/core.py", "line": 10 + i, "url": "u"}]}
            for i, (who, body_i) in enumerate(threads, 1)]
        self.gh.state["repos"][repo]["things"][str(n)] = {
            "number": n, "title": title, "body": body, "headRefName": head, "headRefOid": hist[head][-1],
            "baseRefName": base, "state": "OPEN", "isDraft": False, "isCrossRepository": False, "author": author,
            "labels": list(labels), "assignees": [], "reviewRequests": [], "reviewDecision": "REVIEW_REQUIRED",
            "mergeable": "MERGEABLE", "comments": [], "kind": "pr", "reviews": [], "threads": thread_rows,
            "history_fork": fork}
        if seeded:
            self.prs[(repo, key)] = n
        return n

    def thing(self, repo, n):
        return self.gh.state["repos"][repo]["things"][str(n)]

    def comment(self, repo, n, body):
        t = self.thing(repo, n)
        t["comments"].append({"id": 8000 + len(t["comments"]), "user": "octocat", "assoc": "OWNER", "body": body,
                              "created_at": "2026-01-01T00:00:00Z"})

    def reply(self, repo, n, idx):
        t = self.thing(repo, n)["threads"][idx - 1]
        t["comments"].append({"id": "r", "databaseId": 1, "author": "octocat", "authorAssociation": "OWNER",
                              "body": "done", "path": "calc/core.py", "line": 10, "url": "u"})

    def review(self, repo, n, event, body="", inline=()):
        t = self.thing(repo, n)
        t["reviews"].append({"event": event, "body": body, "id": len(t["reviews"]) + 1})
        t["inline"] = list(inline)

    # -- manifest
    def manifest(self, sid, expect, started="2026-01-01T00:00:00Z", with_companion=False):
        reader = readers.GhReader(self.gh)
        refs = lambda d, repo: [{"key": k, "number": n} for (r, k), n in d.items() if r == repo]
        m = {"run_id": "rid", "scenario": sid, "repo": MAIN, "companion": COMP if with_companion else None,
             "started_at": started,
             "created": {"issues": refs(self.issues, MAIN), "prs": refs(self.prs, MAIN)},
             "companion_created": ({"issues": refs(self.issues, COMP), "prs": refs(self.prs, COMP)}
                                   if with_companion else None),
             "snapshot": reader.snapshot(MAIN),
             "companion_snapshot": reader.snapshot(COMP) if with_companion else None,
             "expect": copy.deepcopy(expect)}
        m["shas"] = {"branches": dict(m["snapshot"]["branches"])}
        return m

    def grade(self, manifest, scenario=None):
        import grade_state
        return grade_state.grade(manifest, scenario or manifest["scenario"], "github", runner=self.gh)


def by_name(summary):
    return {r["name"]: r for r in summary["results"]}
