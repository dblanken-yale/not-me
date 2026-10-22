"""An in-memory GitHub that serves the gh argv subset the not-me client uses.

FakeRunner is the injectable runner; tests/fakes/bin/gh serves the same logic
from a state file so the default subprocess runner can be tested end to end.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple

VALUE_FLAGS = {"-X", "--method", "-R", "--json", "-f", "-F", "--input", "--body-file", "--head", "--base",
               "--title", "--limit", "--label", "--reviewer", "--assignee", "--state", "--add-assignee",
               "--remove-assignee", "--color", "--description"}

Result = Tuple[int, str, str]


def seed_state() -> Dict[str, Any]:
    def issue(number: int, title: str, labels: List[str]) -> Dict[str, Any]:
        return {"number": number, "title": title, "body": f"Body of {title}", "state": "OPEN", "labels": labels,
                "assignees": [], "author": "octocat", "comments": [], "kind": "issue"}

    pr = {"number": 3, "title": "Add widget", "body": "Adds the widget.", "headRefName": "feat/widget",
          "headRefOid": "a" * 40, "baseRefName": "main", "state": "OPEN", "isDraft": False,
          "isCrossRepository": False, "author": "octocat", "labels": ["needs review"], "assignees": [],
          "reviewRequests": [], "reviewDecision": "REVIEW_REQUIRED", "mergeable": "MERGEABLE", "comments": [],
          "kind": "pr", "reviews": [],
          "threads": [{"id": "PRRT_1", "isResolved": False, "comments": [
              {"id": "PRRC_1", "databaseId": 501, "author": "hubot", "authorAssociation": "MEMBER",
               "body": "Please rename this.", "path": "src/widget.py", "line": 12,
               "url": "https://github.com/acme/app/pull/3#discussion_r501"}]}]}
    return {
        "user": "octocat", "next_id": 1000, "timeline": [],
        "repos": {
            "acme/app": {"default_branch": "main", "next_number": 4,
                         "things": {"1": issue(1, "First issue", ["to do"]), "2": issue(2, "Second issue", []),
                                    "3": pr}},
            "acme/app-ui": {"default_branch": "develop", "next_number": 1, "things": {}},
            "acme/other": {"default_branch": "main", "next_number": 1, "things": {}},
        },
        "board": {"owner": "acme", "number": 6, "id": "PVT_1",
                  "status_field": {"id": "PVTSSF_1", "options": [{"id": "opt_todo", "name": "To Do"},
                                                                  {"id": "opt_prog", "name": "In progress"},
                                                                  {"id": "opt_rev", "name": "In review"}]},
                  "items": [{"id": "PVTI_1", "repo": "acme/app", "number": 1, "status": "To Do"}]},
    }


def parse_argv(argv: Sequence[str]) -> Tuple[List[str], Dict[str, List[str]]]:
    positionals: List[str] = []
    flags: Dict[str, List[str]] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in VALUE_FLAGS and i + 1 < len(argv):
            flags.setdefault(a, []).append(argv[i + 1])
            i += 2
            continue
        if a.startswith("-"):
            flags.setdefault(a, [])
        else:
            positionals.append(a)
        i += 1
    return positionals, flags


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _kv(values: List[str]) -> Dict[str, str]:
    return dict(v.split("=", 1) for v in values if "=" in v)


class FakeGitHub:
    def __init__(self, state: Optional[Dict[str, Any]] = None):
        self.state = state if state is not None else seed_state()
        self.race_assignee: Any = None  # set to a login (or True for "hubot"): assigned right after the next issue view
        self.drop_adds = False  # PUT labels silently drops labels that were not already there
        self.fail_next: Optional[str] = None  # stderr text for the next call, which then exits 1
        self.rate_limit = False  # every call fails with a rate limit error
        self._tick = 0  # timeline timestamps count up one second per event
        self.review_state_override: Optional[str] = None  # reviews read back with this state instead of the real one

    # --- plumbing ---

    def _id(self) -> int:
        self.state["next_id"] += 1
        return self.state["next_id"]

    def _repo(self, slug: str) -> Dict[str, Any]:
        repo = self.state["repos"].get(slug)
        if repo is None:
            raise KeyError(f"HTTP 404: Not Found ({slug})")
        return repo

    def _thing(self, slug: str, n: Any) -> Dict[str, Any]:
        thing = self._repo(slug)["things"].get(str(n))
        if thing is None:
            raise KeyError(f"HTTP 404: Not Found ({slug}#{n})")
        return thing

    def _event(self, slug: str, n: Any, event: str, **extra: Any) -> None:
        self._tick += 1
        stamp = f"2026-01-01T{self._tick // 3600:02d}:{self._tick // 60 % 60:02d}:{self._tick % 60:02d}Z"
        entry = {"repo": slug, "number": int(n), "event": event, "created_at": stamp}
        entry.update(extra)
        self.state["timeline"].append(entry)

    # --- helpers for tests ---

    def add_assignee(self, slug: str, n: Any, login: str, created_at: Optional[str] = None) -> None:
        """Assign login and record an `assigned` timeline event (optionally with a chosen timestamp)."""
        thing = self._thing(slug, n)
        if login not in thing["assignees"]:
            thing["assignees"].append(login)
        self._event(slug, n, "assigned", assignee={"login": login})
        if created_at:
            self.state["timeline"][-1]["created_at"] = created_at

    def post_comment(self, slug: str, n: Any, user: str, body: str, created_at: str = "2026-01-01T00:00:00Z") -> int:
        thing = self._thing(slug, n)
        cid = self._id()
        thing["comments"].append({"id": cid, "user": user, "assoc": "MEMBER", "body": body,
                                  "created_at": created_at})
        self._event(slug, n, "commented", id=cid)
        return cid

    def serve(self, argv: Sequence[str]) -> Result:
        if self.rate_limit:
            return 1, "", "gh: API rate limit exceeded (HTTP 403)"
        if self.fail_next is not None:
            msg, self.fail_next = self.fail_next, None
            return 1, "", msg
        try:
            out = self._dispatch(list(argv))
        except KeyError as exc:
            return 1, "", str(exc.args[0])
        if out is None:
            return 1, "", f"fakegh: unsupported argv: gh {' '.join(argv)}"
        return 0, out if isinstance(out, str) else json.dumps(out), ""

    # --- views ---

    @staticmethod
    def _view(thing: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
        full = {
            "number": thing["number"], "title": thing["title"], "body": thing["body"], "state": thing["state"],
            "labels": [{"name": n} for n in thing["labels"]], "assignees": [{"login": a} for a in thing["assignees"]],
            "author": thing["author"] if isinstance(thing["author"], dict) else {"login": thing["author"]},
            "url": f"https://github.com/acme/app/issues/{thing['number']}",
            "createdAt": thing.get("created_at", f"2026-01-01T00:00:{thing['number'] % 60:02d}Z"),
            # `gh --json comments` drops the type and the "[bot]" suffix, as the real gh does.
            "comments": [{"id": c["id"], "author": {"login": c["user"][:-5] if c["user"].endswith("[bot]") else c["user"]},
                          "authorAssociation": c["assoc"],
                          "body": c["body"]} for c in thing["comments"]],
        }
        if thing["kind"] == "pr":
            for k in ("headRefName", "headRefOid", "baseRefName", "isDraft", "isCrossRepository",
                      "reviewDecision", "mergeable"):
                full[k] = thing[k]
            full["reviewRequests"] = [{"login": r} for r in thing["reviewRequests"]]
            full["statusCheckRollup"] = thing.get("statusCheckRollup", [])
        unknown = [f for f in fields if f not in full]
        if unknown:
            raise KeyError(f"Unknown JSON field: {unknown[0]!r}")
        return {f: full[f] for f in fields}

    # --- dispatch ---

    def _dispatch(self, argv: List[str]) -> Any:
        pos, flags = parse_argv(argv)
        if not pos:
            return None
        head = pos[0]
        if head == "api":
            return self._api(pos[1:], flags)
        verb = pos[1] if len(pos) > 1 else ""
        repo_flag = (flags.get("-R") or [None])[0]
        fields = [f for v in flags.get("--json", []) for f in v.split(",")]
        if head == "repo" and verb == "view":
            return {"defaultBranchRef": {"name": self._repo(pos[2])["default_branch"]}}
        if head == "issue" and verb == "view":
            thing = self._thing(repo_flag, pos[2])
            out = self._view(thing, fields)
            if self.race_assignee:
                login = "hubot" if self.race_assignee is True else self.race_assignee
                self.race_assignee = None
                self.add_assignee(repo_flag, pos[2], login)
            return out
        if head == "issue" and verb == "list":
            wanted = [t for t in self._repo(repo_flag)["things"].values() if t["kind"] == "issue"]
            state = (flags.get("--state") or ["open"])[0].upper()
            wanted = [t for t in wanted if state == "ALL" or t["state"] == state]
            for label in flags.get("--label", []):
                wanted = [t for t in wanted if label in t["labels"]]
            for who in flags.get("--assignee", []):
                wanted = [t for t in wanted if who in t["assignees"]]
            limit = int((flags.get("--limit") or ["30"])[0])
            return [self._view(t, fields) for t in wanted[:limit]]
        if head == "issue" and verb == "edit":
            thing = self._thing(repo_flag, pos[2])
            if "--body-file" in flags:
                thing["body"] = _read(flags["--body-file"][0])
            for login in flags.get("--add-assignee", []):
                self.add_assignee(repo_flag, pos[2], login)
            for login in flags.get("--remove-assignee", []):
                if login in thing["assignees"]:
                    thing["assignees"].remove(login)
                    self._event(repo_flag, pos[2], "unassigned", assignee={"login": login})
            return f"https://github.com/{repo_flag}/issues/{pos[2]}\n"
        if head == "issue" and verb == "comment":
            return self._comment(repo_flag, pos[2], _read(flags["--body-file"][0]))
        if head == "label" and verb == "list":
            defs = self._repo(repo_flag).get("label_defs", {})
            return [{"name": n, **d} for n, d in defs.items()][:int((flags.get("--limit") or ["30"])[0])]
        if head == "label" and verb == "create":
            self._repo(repo_flag).setdefault("label_defs", {})[pos[2]] = {
                "color": (flags.get("--color") or [""])[0], "description": (flags.get("--description") or [""])[0]}
            return f"https://github.com/{repo_flag}/labels/{pos[2]}\n"
        if head == "pr" and verb == "view":
            return self._view(self._thing(repo_flag, pos[2]), fields)
        if head == "pr" and verb == "list":
            wanted = [t for t in self._repo(repo_flag)["things"].values() if t["kind"] == "pr"]
            state = (flags.get("--state") or ["open"])[0].upper()
            wanted = [t for t in wanted if state == "ALL" or t["state"] == state]
            for head_ref in flags.get("--head", []):
                wanted = [t for t in wanted if t["headRefName"] == head_ref]
            for base_ref in flags.get("--base", []):
                wanted = [t for t in wanted if t["baseRefName"] == base_ref]
            for label in flags.get("--label", []):
                wanted = [t for t in wanted if label in t["labels"]]
            for who in flags.get("--assignee", []):
                wanted = [t for t in wanted if who in t["assignees"]]
            return [self._view(t, fields) for t in wanted]
        if head == "pr" and verb == "checks":
            thing = self._thing(repo_flag, pos[2])
            return thing["checks"] if "checks" in thing else None  # no "checks" key: unsupported, exits 1
        if head == "pr" and verb == "create":
            return self._pr_create(repo_flag, flags)
        if head == "issue" and verb == "create":
            return self._issue_create(repo_flag, flags)
        if head == "pr" and verb == "edit":
            thing = self._thing(repo_flag, pos[2])
            thing["body"] = _read(flags["--body-file"][0])
            return f"https://github.com/{repo_flag}/pull/{pos[2]}\n"
        return None

    def _comment(self, slug: str, n: Any, body: str) -> str:
        thing = self._thing(slug, n)
        cid = self._id()
        thing["comments"].append({"id": cid, "user": self.state["user"], "assoc": "OWNER", "body": body,
                                  "created_at": "2026-01-01T00:00:00Z"})
        self._event(slug, n, "commented", id=cid)
        return f"https://github.com/{slug}/issues/{n}#issuecomment-{cid}\n"

    def _issue_create(self, slug: str, flags: Dict[str, List[str]]) -> str:
        repo = self._repo(slug)
        number = repo["next_number"]
        repo["next_number"] += 1
        repo["things"][str(number)] = {
            "number": number, "title": flags["--title"][0], "body": _read(flags["--body-file"][0]),
            "state": "OPEN", "labels": list(flags.get("--label", [])), "assignees": [],
            "author": self.state["user"], "comments": [], "kind": "issue"}
        self._event(slug, number, "issue_created")
        return f"https://github.com/{slug}/issues/{number}\n"

    def _pr_create(self, slug: str, flags: Dict[str, List[str]]) -> str:
        repo = self._repo(slug)
        number = repo["next_number"]
        repo["next_number"] += 1
        repo["things"][str(number)] = {
            "number": number, "title": flags["--title"][0], "body": _read(flags["--body-file"][0]),
            "headRefName": flags["--head"][0], "headRefOid": "b" * 40, "baseRefName": flags["--base"][0],
            "state": "OPEN", "isDraft": "--draft" in flags, "isCrossRepository": False,
            "author": self.state["user"], "labels": list(flags.get("--label", [])),
            "assignees": list(flags.get("--assignee", [])), "reviewRequests": list(flags.get("--reviewer", [])),
            "reviewDecision": "REVIEW_REQUIRED", "mergeable": "MERGEABLE", "comments": [], "kind": "pr",
            "reviews": [], "threads": []}
        self._event(slug, number, "pr_created")
        return f"https://github.com/{slug}/pull/{number}\n"

    def _find_comment(self, slug: str, comment_id: int) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        for thing in self._repo(slug)["things"].values():
            for c in thing["comments"]:
                if c["id"] == comment_id:
                    return thing, c
        raise KeyError("HTTP 404: Not Found (comment)")

    def _find_thread(self, thread_id: Optional[str] = None, comment_id: Optional[int] = None,
                     slug: Optional[str] = None) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        """(repo, pr, thread) matching a thread id or one of its comments' database ids."""
        for repo_slug, repo in self.state["repos"].items():
            if slug not in (None, repo_slug):
                continue
            for thing in repo["things"].values():
                for t in thing.get("threads", []):
                    if t["id"] == thread_id or any(c["databaseId"] == comment_id for c in t["comments"]):
                        return repo_slug, thing, t
        raise KeyError("HTTP 404: Not Found (thread)")

    # (method, endpoint regex, handler); first match wins
    def _routes(self) -> List[Tuple[str, str, Any]]:
        r = r"repos/([^/]+/[^/]+)"
        return [
            ("GET", r"user", self._r_user),
            ("GET", r + r"/issues/(\d+)/labels", self._r_labels_get),
            ("PUT", r + r"/issues/(\d+)/labels", self._r_labels_put),
            ("GET", r + r"/issues/(\d+)/comments", self._r_comments),
            ("GET", r + r"/issues/(\d+)/timeline", self._r_timeline),
            ("PATCH", r + r"/issues/comments/(\d+)", self._r_comment_edit),
            ("POST", r + r"/pulls/(\d+)/comments/(\d+)/replies", self._r_reply),
            ("GET", r + r"/pulls/(\d+)/reviews", self._r_reviews),
            ("POST", r + r"/pulls/(\d+)/reviews", self._r_review),
            ("POST", r + r"/pulls/(\d+)/requested_reviewers", self._r_request_review),
            ("GET", r + r"/deployments", self._r_deployments),
            ("GET", r + r"/deployments/(\d+)/statuses", self._r_deployment_statuses),
        ]

    def _api(self, pos: List[str], flags: Dict[str, List[str]]) -> Any:
        if not pos:
            return None
        endpoint = pos[0].split("?")[0].strip("/")
        self._query = dict(kv.split("=", 1) for kv in pos[0].partition("?")[2].split("&") if "=" in kv)
        method = (flags.get("-X") or flags.get("--method") or ["GET"])[0].upper()
        if endpoint == "graphql":
            return self._graphql(self._fields(flags))
        for route_method, pattern, handler in self._routes():
            m = re.fullmatch(pattern, endpoint)
            if m and route_method == method:
                return handler(m.groups(), flags)
        return None

    @staticmethod
    def _fields(flags: Dict[str, List[str]]) -> Dict[str, str]:
        """-f/-F key=value pairs; a value of @path is read from that file, like gh does."""
        fields = _kv(flags.get("-f", []) + flags.get("-F", []))
        return {k: _read(v[1:]) if v.startswith("@") else v for k, v in fields.items()}

    def _r_user(self, g, flags):
        return {"login": self.state["user"], "id": 1, "type": "User"}

    def _r_labels_get(self, g, flags):
        return [{"name": n} for n in self._thing(*g)["labels"]]

    def _r_labels_put(self, g, flags):
        thing = self._thing(*g)
        wanted = json.loads(_read(flags["--input"][0]))["labels"]
        if self.drop_adds:
            wanted = [n for n in wanted if n in thing["labels"]]
        thing["labels"] = list(wanted)
        self._event(g[0], g[1], "labels_set", labels=list(wanted))
        return [{"name": n} for n in wanted]

    def _r_comments(self, g, flags):
        # REST spells a bot "<slug>[bot]" and gives its type, as GitHub does.
        return [{"id": c["id"], "user": {"login": c["user"], "type": "Bot" if c["user"].endswith("[bot]") else "User"},
                 "author_association": c["assoc"], "body": c["body"],
                 "created_at": c["created_at"], "updated_at": c["created_at"],
                 "html_url": f"https://github.com/{g[0]}/issues/{g[1]}#issuecomment-{c['id']}"}
                for c in self._thing(*g)["comments"]]

    def _r_timeline(self, g, flags):
        events = [e for e in self.state["timeline"] if e["repo"] == g[0] and e["number"] == int(g[1])]
        query = getattr(self, "_query", {})
        if "per_page" in query:
            size, page = int(query["per_page"]), int(query.get("page", 1))
            return events[(page - 1) * size:page * size]
        return events

    def _r_comment_edit(self, g, flags):
        thing, comment = self._find_comment(g[0], int(g[1]))
        comment["body"] = self._body_field(flags)
        self._event(g[0], thing["number"], "comment_edited", id=comment["id"])
        return {"id": comment["id"], "body": comment["body"]}

    def _r_reply(self, g, flags):
        slug, n, comment_id = g
        _, _, thread = self._find_thread(comment_id=int(comment_id), slug=slug)
        first = thread["comments"][0]
        cid = self._id()
        thread["comments"].append({
            "id": f"PRRC_{cid}", "databaseId": cid, "author": self.state["user"], "authorAssociation": "OWNER",
            "body": self._body_field(flags), "path": first["path"], "line": first["line"],
            "url": f"https://github.com/{slug}/pull/{n}#discussion_r{cid}"})
        self._event(slug, n, "thread_reply", id=cid)
        return {"id": cid}

    def _r_review(self, g, flags):
        data = json.loads(_read(flags["--input"][0]))
        rid = self._id()
        self._thing(*g)["reviews"].append({**data, "id": rid, "commit_id": self._thing(*g).get("headRefOid"),
                                           "submitted_at": "2026-01-01T00:00:00Z"})
        self._event(g[0], g[1], "reviewed", state=data.get("event"))
        return {"id": rid, "state": data.get("event")}

    def _r_reviews(self, g, flags):
        states = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}
        return [{"id": r["id"], "user": {"login": r.get("user") or self.state["user"]}, "body": r.get("body", ""),
                 "commit_id": r.get("commit_id"), "submitted_at": r.get("submitted_at"),
                 "state": self.review_state_override or r.get("state") or states.get(r.get("event"), r.get("event"))}
                for r in self._thing(*g)["reviews"]]

    def _r_deployments(self, g, flags):
        sha = getattr(self, "_query", {}).get("sha")
        return [{"id": d["id"], "sha": d["sha"]} for d in self.state.get("deployments", [])
                if d["repo"] == g[0] and sha in (None, d["sha"])]

    def _r_deployment_statuses(self, g, flags):
        for d in self.state.get("deployments", []):
            if d["repo"] == g[0] and d["id"] == int(g[1]):
                return list(d["statuses"])
        return []

    def _r_request_review(self, g, flags):
        pr = self._thing(*g)
        pr["reviewRequests"] += [v.split("=", 1)[1] for v in flags.get("-f", []) if v.startswith("reviewers[]=")]
        self._event(g[0], g[1], "review_requested")
        return {"number": pr["number"]}

    def _body_field(self, flags: Dict[str, List[str]]) -> str:
        fields = self._fields(flags)
        if "body" not in fields:
            raise KeyError("HTTP 422: body is required")
        return fields["body"]

    def _graphql(self, variables: Dict[str, str]) -> Any:
        query = variables.get("query", "")
        if "resolveReviewThread" in query:
            slug, pr, thread = self._find_thread(thread_id=variables.get("id"))
            thread["isResolved"] = True
            self._event(slug, pr["number"], "thread_resolved", id=thread["id"])
            return {"data": {"resolveReviewThread": {"thread": {"id": thread["id"], "isResolved": True}}}}
        if "node(id" in query and "PullRequestReviewThread" in query:
            slug, _, _ = self._find_thread(thread_id=variables.get("id"))
            return {"data": {"node": {"repository": {"nameWithOwner": slug}}}}
        if "reviewThreads" in query:
            number = re.search(r"pullRequest\(number:(\d+)\)", query).group(1)
            pr = self._thing(f"{variables['owner']}/{variables['name']}", number)
            def author(c: Dict[str, Any]) -> Any:  # a dict (with __typename) passes through; None stays null
                a = c["author"]
                return a if isinstance(a, dict) or a is None else {"login": a}

            nodes = [{"id": t["id"], "isResolved": t["isResolved"], "isOutdated": t.get("isOutdated", False),
                      "comments": {"nodes": [{**c, "author": author(c)} for c in t["comments"]]}}
                     for t in pr.get("threads", [])]
            reviews = [{**r, "author": author(r)} for r in pr.get("review_nodes", [])]
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes},
                                                            "reviews": {"nodes": reviews}}}}}
        board = self.state["board"]
        if "updateProjectV2ItemFieldValue" in query:
            names = {o["id"]: o["name"] for o in board["status_field"]["options"]}
            for item in board["items"]:
                if item["id"] == variables.get("item") and variables.get("option") in names \
                        and variables.get("project") == board["id"] \
                        and variables.get("field") == board["status_field"]["id"]:
                    item["status"] = names[variables["option"]]
                    return {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": item["id"]}}}}
            raise KeyError("GraphQL: could not update the item")
        if "projectV2(number" in query:
            number = int(re.search(r"projectV2\(number:(\d+)\)", query).group(1))
            if variables.get("owner") != board["owner"] or number != board["number"]:
                return {"data": {"repositoryOwner": {"projectV2": None}}}
            project: Dict[str, Any] = {"id": board["id"]}
            if "createdAt" in query:  # the rich board query: item content and priority fields
                project["field"] = board["status_field"]
                project["items"] = {"nodes": [self._rich_item(i, query) for i in board["items"]]}
            elif "items(" in query:
                project["field"] = board["status_field"]
                project["items"] = {"nodes": [
                    {"id": i["id"], "fieldValueByName": {"name": i["status"]},
                     "content": {"number": i["number"], "repository": {"nameWithOwner": i["repo"]}}}
                    for i in board["items"]]}
            return {"data": {"repositoryOwner": {"projectV2": project}}}
        return None


    def _rich_item(self, item: Dict[str, Any], query: str) -> Dict[str, Any]:
        """A board item with issue content and one aliased value per `pN:fieldValueByName(name:"X")`."""
        node: Dict[str, Any] = {"id": item["id"], "fieldValueByName": {"name": item["status"]}}
        for alias, field_name in re.findall(r'(p\d+):fieldValueByName\(name:"([^"]+)"\)', query):
            value = (item.get("fields") or {}).get(field_name)
            node[alias] = {"name": value} if value else None
        thing = self._repo(item["repo"])["things"].get(str(item["number"]))
        if thing is not None:
            view = self._view(thing, ["number", "title", "url", "state", "createdAt"])
            node["content"] = {**view, "author": {"login": thing["author"]},
                               "labels": {"nodes": [{"name": n} for n in thing["labels"]]},
                               "assignees": {"nodes": [{"login": a} for a in thing["assignees"]]},
                               "repository": {"nameWithOwner": item["repo"]}}
        else:
            node["content"] = None
        return node


class FakeRunner(FakeGitHub):
    """Callable runner (argv -> CompletedProcess) that records every call."""

    def __init__(self, state: Optional[Dict[str, Any]] = None):
        super().__init__(state)
        self.calls: List[List[str]] = []

    def __call__(self, argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(argv))
        code, out, err = self.serve(argv)
        return subprocess.CompletedProcess(["gh", *argv], code, out, err)

    def writes_made(self) -> List[List[str]]:
        """Recorded calls that are not plain reads."""
        from notme.gh import is_read
        return [c for c in self.calls if not is_read(c)]

    def snapshot(self) -> Dict[str, Any]:
        return copy.deepcopy(self.state)
