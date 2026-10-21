#!/usr/bin/env python3
"""Seed, reset and verify the not-me eval fixture repositories.

Usage:
  seed_fixture.py --repo O/N [--companion O/N] --owner O \
      (--bootstrap | --reset | --scenario ID --run-id RID | --teardown RID | --verify)
      [--dry-run]

The fixture repos are private throwaways. Evals run not-me skills against them,
so before each scenario they must be in an exact known state, and after it they
must be cleaned up.

Authority note: this script is the ONLY actor allowed to force-move the `main`
and `develop` refs of a fixture repo (back to the `fixture-baseline` tag commit)
and to delete branches and issues there. That is acceptable only because every
repo it touches is checked first: the name must end in `-fixture` or
`-fixture-companion`, the owner must equal --owner / NOT_ME_FIXTURE_OWNER, and
`gh repo view` must report the repo as private. The checks run in --dry-run too.

Modes:
  --bootstrap  one-time: push fixture/repo (and fixture/companion) as the first
               commit through a temporary clone, tag it `fixture-baseline`,
               create `develop`, and sync labels.
  --reset      take the lock, close open PRs, delete every branch except main,
               develop and fixture-baseline, delete all issues, force main and
               develop to the baseline commit, sync labels from labels.json, then
               read everything back (exit 1 on any mismatch). The lock is kept
               when --run-id is given and released otherwise.
  --scenario   apply fixture/scenarios/<ID>.json (patches, issues, PRs, review
               threads, comments, reviews) and write evals/.state/<RID>/<ID>.json.
  --teardown   check the lock belongs to RID, clean up as --reset does, release it.
  --verify     compare the live repo to a baseline snapshot, or to the snapshot in
               --manifest PATH, and print the differences (exit 1 if any).

Every external call goes through one `run(argv)` helper so tests can inject a
fake. --dry-run prints each command it would run, prefixed with "+", and runs
nothing (reads return placeholders).
"""

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

LOCK_TITLE = "eval-lock"
LOCK_RE = re.compile(r"<!--\s*lock:(\S+)\s+ts=(\S+)\s*-->")
LOCK_STALE = timedelta(hours=2)
BASELINE_TAG = "fixture-baseline"
KEEP_BRANCHES = ("main", "develop", BASELINE_TAG)
BOT_WORKFLOW = "post-as-bot.yml"
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

Result = namedtuple("Result", "returncode stdout stderr")


class SeedError(Exception):
    """Something went wrong while seeding (exit 1)."""


class SafetyError(SeedError):
    """A safety check refused the operation (exit 2)."""


class LockError(SeedError):
    """The eval lock is held by someone else (exit 2)."""


class MismatchError(SeedError):
    """Read-back found the repo not in the expected state (exit 1)."""

    def __init__(self, diffs):
        super().__init__("read-back mismatch:\n  " + "\n  ".join(diffs))
        self.diffs = diffs


def default_run(argv, input_text=None):
    proc = subprocess.run(
        argv,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return Result(proc.returncode, proc.stdout, proc.stderr)


def utcnow():
    return datetime.now(timezone.utc)


def fmt_cmd(argv):
    return " ".join(shlex.quote(str(a)) for a in argv)


# --------------------------------------------------------------------------
# safety


def check_slug(slug, owner):
    """Refuse anything that is not a fixture repo owned by `owner`."""
    if not owner:
        raise SafetyError("owner required: pass --owner or set NOT_ME_FIXTURE_OWNER")
    match = re.fullmatch(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", slug or "")
    if not match:
        raise SafetyError("repo must look like owner/name, got %r" % (slug,))
    repo_owner, name = match.groups()
    if repo_owner.lower() != owner.lower():
        raise SafetyError(
            "refusing %s: owner %r is not the allowed owner %r" % (slug, repo_owner, owner)
        )
    if not (name.endswith("-fixture") or name.endswith("-fixture-companion")):
        raise SafetyError(
            "refusing %s: name must end with -fixture or -fixture-companion" % slug
        )


# --------------------------------------------------------------------------
# command client


class Client:
    """Runs commands through an injectable `run`, logging every write."""

    def __init__(self, run=default_run, dry_run=False, out=None, err=None,
                 sleep=time.sleep, now=utcnow):
        self.run = run
        self.dry_run = dry_run
        self.out = out if out is not None else sys.stdout
        self.err = err if err is not None else sys.stderr
        self.sleep = sleep
        self.now = now
        self.writes = []
        self._fake_number = 0

    def _say(self, argv):
        print("+ " + fmt_cmd(argv), file=self.out)

    def read(self, argv, default="", check=True):
        """Run a read-only command and return stdout. Dry-run returns `default`."""
        if self.dry_run:
            self._say(argv)
            return default
        res = self.run(argv)
        if check and res.returncode != 0:
            raise SeedError("command failed: %s\n%s" % (fmt_cmd(argv), res.stderr.strip()))
        return res.stdout

    def probe(self, argv):
        """Return True if a read-only command exits 0. Dry-run returns False."""
        if self.dry_run:
            self._say(argv)
            return False
        return self.run(argv).returncode == 0

    def json(self, argv, default):
        text = self.read(argv, default=json.dumps(default))
        text = text.strip()
        if not text:
            return default
        try:
            return json.loads(text)
        except ValueError as exc:
            raise SeedError("bad JSON from %s: %s" % (fmt_cmd(argv), exc))

    def write(self, argv, default="", input_text=None):
        """Run a state-changing command; always logged. Dry-run only prints."""
        if self.dry_run:
            self._say(argv)
            return default
        line = fmt_cmd(argv)
        self.writes.append(line)
        print("WRITE " + line, file=self.err)
        res = self.run(argv, input_text) if input_text is not None else self.run(argv)
        if res.returncode != 0:
            raise SeedError("command failed: %s\n%s" % (line, res.stderr.strip()))
        return res.stdout

    def fake_number(self):
        self._fake_number += 1
        return self._fake_number


def number_from_url(text):
    match = re.search(r"/(\d+)\s*$", (text or "").strip())
    if not match:
        raise SeedError("could not read a number from %r" % (text,))
    return int(match.group(1))


def count(value):
    if isinstance(value, list):
        return len(value)
    return int(value or 0)


def names(items, key):
    return sorted(i[key] if isinstance(i, dict) else i for i in (items or []))


# --------------------------------------------------------------------------
# snapshots


def flatten(obj, prefix=""):
    flat = {}
    if isinstance(obj, dict):
        for key in sorted(obj):
            flat.update(flatten(obj[key], "%s/%s" % (prefix, key)))
    elif isinstance(obj, list):
        flat[prefix] = json.dumps(obj, sort_keys=True)
    else:
        flat[prefix] = json.dumps(obj)
    if not flat and prefix:
        flat[prefix] = "{}"
    return flat


def diff_snapshots(expected, actual):
    """Return human-readable differences between two snapshots."""
    exp, act = flatten(expected), flatten(actual)
    out = []
    for key in sorted(set(exp) | set(act)):
        if key not in act:
            out.append("missing   %s (expected %s)" % (key, exp[key]))
        elif key not in exp:
            out.append("unexpected %s = %s" % (key, act[key]))
        elif exp[key] != act[key]:
            out.append("changed   %s: expected %s, got %s" % (key, exp[key], act[key]))
    return out


def load_labels():
    with open(str(HERE / "labels.json"), encoding="utf-8") as fh:
        return json.load(fh)


def load_scenario(sid):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sid or ""):
        raise SeedError("bad scenario id %r" % (sid,))
    path = HERE / "scenarios" / ("%s.json" % sid)
    if not path.is_file():
        raise SeedError("no such scenario: %s" % path)
    with open(str(path), encoding="utf-8") as fh:
        return json.load(fh)


def subst(value, mapping):
    """Replace {rid}/{id} tokens in every string."""
    if isinstance(value, str):
        for key, rep in mapping.items():
            value = value.replace("{%s}" % key, rep)
        return value
    if isinstance(value, list):
        return [subst(v, mapping) for v in value]
    if isinstance(value, dict):
        return {
            k: (v if k in ("find", "replace") else subst(v, mapping))
            for k, v in value.items()
        }
    return value


# --------------------------------------------------------------------------
# one fixture repository


class Fixture:
    def __init__(self, client, repo):
        self.c = client
        self.repo = repo

    # helpers
    def api(self, path):
        return "repos/%s/%s" % (self.repo, path)

    def gh(self, *args):
        return ["gh"] + list(args)

    # ---- safety
    def check_private(self):
        data = self.c.json(
            self.gh("repo", "view", self.repo, "--json", "isPrivate"),
            {"isPrivate": True},
        )
        if data.get("isPrivate") is not True:
            raise SafetyError("refusing %s: repo is not private" % self.repo)

    # ---- lock
    def _locks(self):
        items = self.c.json(
            self.gh("issue", "list", "--repo", self.repo, "--state", "open",
                    "--limit", "1000", "--json", "number,title,body"),
            [],
        )
        return sorted((i for i in items if i.get("title") == LOCK_TITLE),
                      key=lambda i: i["number"])

    @staticmethod
    def parse_lock(body):
        match = LOCK_RE.search(body or "")
        if not match:
            return None, None
        try:
            ts = datetime.strptime(match.group(2), TS_FORMAT).replace(tzinfo=timezone.utc)
        except ValueError:
            return match.group(1), None
        return match.group(1), ts

    def _lock_body(self, rid):
        return "<!-- lock:%s ts=%s -->" % (rid, self.c.now().strftime(TS_FORMAT))

    def _stale(self, ts):
        return ts is None or self.c.now() - ts >= LOCK_STALE

    def acquire(self, rid):
        """Take the eval lock (compare-and-set via edit then re-read)."""
        locks = self._locks()
        if locks:
            first = locks[0]
            holder, ts = self.parse_lock(first.get("body"))
            if holder == rid:
                return first["number"]
            if holder and not self._stale(ts):
                raise LockError("eval-lock held by %s since %s" % (holder, ts))
            self.c.write(self.gh("issue", "edit", str(first["number"]), "--repo", self.repo,
                                 "--body", self._lock_body(rid)))
            fresh = self.c.json(
                self.gh("issue", "view", str(first["number"]), "--repo", self.repo,
                        "--json", "body"),
                {"body": self._lock_body(rid)},
            )
            if self.parse_lock(fresh.get("body"))[0] != rid:
                raise LockError("lost the eval-lock race on issue %s" % first["number"])
            return first["number"]
        out = self.c.write(
            self.gh("issue", "create", "--repo", self.repo, "--title", LOCK_TITLE,
                    "--body", self._lock_body(rid)),
            default="https://github.com/%s/issues/%d" % (self.repo, self.c.fake_number()),
        )
        number = number_from_url(out)
        if self.c.dry_run:
            return number
        locks = self._locks()
        if locks and locks[0]["number"] != number:
            self.c.write(self.gh("issue", "delete", str(number), "--repo", self.repo, "--yes"))
            raise LockError("lost the eval-lock race to issue %s" % locks[0]["number"])
        return number

    def release(self, rid, force=False):
        for lock in self._locks():
            holder, ts = self.parse_lock(lock.get("body"))
            if holder != rid and not force and holder and not self._stale(ts):
                raise LockError("eval-lock is held by %s, not %s" % (holder, rid))
            self.c.write(self.gh("issue", "delete", str(lock["number"]), "--repo",
                                 self.repo, "--yes"))

    # ---- refs
    def branches(self):
        out = self.c.read(
            self.gh("api", self.api("branches"), "--paginate", "--jq",
                    '.[] | .name + " " + .commit.sha'),
            default="",
        )
        result = {}
        for line in out.splitlines():
            if line.strip():
                name, sha = line.split()
                result[name] = sha
        return result

    def baseline_sha(self):
        out = self.c.read(
            self.gh("api", self.api("git/ref/tags/" + BASELINE_TAG), "--jq",
                    '.object.type + " " + .object.sha'),
            default="commit <baseline-sha>",
            check=False,
        )
        if not out.strip():
            raise SeedError("tag %s not found in %s; run --bootstrap first"
                            % (BASELINE_TAG, self.repo))
        kind, sha = out.split()
        if kind == "tag":
            sha = self.c.read(self.gh("api", self.api("git/tags/" + sha), "--jq",
                                      ".object.sha")).strip()
        return sha

    # ---- snapshot
    def snapshot(self):
        issues = self.c.json(
            self.gh("issue", "list", "--repo", self.repo, "--state", "all", "--limit",
                    "1000", "--json", "number,title,state,labels,assignees,comments"),
            [],
        )
        prs = self.c.json(
            self.gh("pr", "list", "--repo", self.repo, "--state", "open", "--limit",
                    "1000", "--json",
                    "number,title,state,headRefName,baseRefName,headRefOid,labels,"
                    "assignees,comments,reviews"),
            [],
        )
        labels = self.c.json(
            self.gh("label", "list", "--repo", self.repo, "--limit", "200", "--json",
                    "name,color,description"),
            [],
        )
        return {
            "branches": self.branches(),
            "labels": {
                l["name"]: {"color": l["color"].lower().lstrip("#"),
                            "description": l.get("description") or ""}
                for l in labels
            },
            "issues": {
                str(i["number"]): {
                    "title": i["title"],
                    "state": i["state"],
                    "labels": names(i.get("labels"), "name"),
                    "assignees": names(i.get("assignees"), "login"),
                    "comments": count(i.get("comments")),
                }
                for i in issues if i["title"] != LOCK_TITLE
            },
            "prs": {
                str(p["number"]): {
                    "title": p["title"],
                    "state": p["state"],
                    "head": p["headRefName"],
                    "base": p["baseRefName"],
                    "head_sha": p.get("headRefOid", ""),
                    "labels": names(p.get("labels"), "name"),
                    "assignees": names(p.get("assignees"), "login"),
                    "comments": count(p.get("comments")),
                    "reviews": count(p.get("reviews")),
                }
                for p in prs
            },
        }

    def baseline_snapshot(self, sha):
        return {
            "branches": {"main": sha, "develop": sha},
            "labels": {
                l["name"]: {"color": l["color"].lower().lstrip("#"),
                            "description": l["description"]}
                for l in load_labels()
            },
            "issues": {},
            "prs": {},
        }

    # ---- labels
    def sync_labels(self):
        wanted = {l["name"]: l for l in load_labels()}
        have = {
            l["name"]: l
            for l in self.c.json(
                self.gh("label", "list", "--repo", self.repo, "--limit", "200",
                        "--json", "name,color,description"),
                [],
            )
        }
        for name, want in wanted.items():
            cur = have.get(name)
            if cur is None:
                self.c.write(self.gh("label", "create", name, "--repo", self.repo,
                                     "--color", want["color"],
                                     "--description", want["description"]))
            elif (cur["color"].lower().lstrip("#") != want["color"].lower()
                  or (cur.get("description") or "") != want["description"]):
                self.c.write(self.gh("label", "edit", name, "--repo", self.repo,
                                     "--color", want["color"],
                                     "--description", want["description"]))
        for name in sorted(set(have) - set(wanted)):
            self.c.write(self.gh("label", "delete", name, "--repo", self.repo, "--yes"))

    # ---- reset
    def clean(self):
        """Close PRs, delete branches and issues, force refs, sync labels."""
        prs = self.c.json(
            self.gh("pr", "list", "--repo", self.repo, "--state", "open", "--limit",
                    "1000", "--json", "number,headRefName"),
            [],
        )
        for pr in prs:
            self.c.write(self.gh("pr", "close", str(pr["number"]), "--repo", self.repo))
        existing = self.branches()
        for name in sorted(existing):
            if name not in KEEP_BRANCHES:
                self.c.write(self.gh("api", "-X", "DELETE",
                                     self.api("git/refs/heads/" + name)))
        issues = self.c.json(
            self.gh("issue", "list", "--repo", self.repo, "--state", "all", "--limit",
                    "1000", "--json", "number,title"),
            [],
        )
        for issue in issues:
            if issue["title"] != LOCK_TITLE:
                self.c.write(self.gh("issue", "delete", str(issue["number"]), "--repo",
                                     self.repo, "--yes"))
        sha = self.baseline_sha()
        for branch in ("main", "develop"):
            if branch in existing or self.c.dry_run or branch == "main":
                self.c.write(self.gh("api", "-X", "PATCH",
                                     self.api("git/refs/heads/" + branch),
                                     "-f", "sha=" + sha, "-F", "force=true"))
            else:
                self.c.write(self.gh("api", "-X", "POST", self.api("git/refs"),
                                     "-f", "ref=refs/heads/" + branch, "-f", "sha=" + sha))
        self.sync_labels()
        return sha

    def readback(self, sha):
        return diff_snapshots(self.baseline_snapshot(sha), self.snapshot())

    def reset(self):
        sha = self.clean()
        if self.c.dry_run:
            self.readback(sha)  # prints the read commands
            return
        diffs = self.readback(sha)
        if diffs:
            raise MismatchError(diffs)

    # ---- bootstrap
    def bootstrap(self, source):
        if self.c.probe(self.gh("api", self.api("git/ref/tags/" + BASELINE_TAG))):
            raise SeedError("%s already has tag %s; refusing to bootstrap again"
                            % (self.repo, BASELINE_TAG))
        source = Path(source)
        if not source.is_dir():
            raise SeedError("missing source directory %s" % source)
        tmp = "<tmp>" if self.c.dry_run else tempfile.mkdtemp(prefix="fixture-boot-")
        clone = os.path.join(tmp, "clone")
        git = ["git", "-C", clone, "-c", "user.name=not-me-fixture",
               "-c", "user.email=fixture@example.invalid"]
        try:
            self.c.write(self.gh("repo", "clone", self.repo, clone))
            if not self.c.dry_run:
                for item in sorted(source.iterdir()):
                    if item.name == "__pycache__":
                        continue
                    # `_github` is stored under that name so GitHub does not run
                    # these workflows from the plugin repo; restore it here.
                    name = ".github" if item.name == "_github" else item.name
                    dest = os.path.join(clone, name)
                    if item.is_dir():
                        shutil.copytree(str(item), dest,
                                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                    else:
                        shutil.copy2(str(item), dest)
            self.c.write(git + ["checkout", "-B", "main"])
            self.c.write(git + ["add", "-A"])
            self.c.write(git + ["commit", "-m", "chore: seed fixture baseline"])
            self.c.write(git + ["push", "origin", "main"])
            self.c.write(git + ["tag", BASELINE_TAG])
            self.c.write(git + ["push", "origin", BASELINE_TAG])
            self.c.write(git + ["push", "origin", "main:develop"])
        finally:
            if not self.c.dry_run:
                shutil.rmtree(tmp, ignore_errors=True)
        self.sync_labels()

    # ---- scenario pieces
    def branch_sha(self, branch):
        return self.c.read(
            self.gh("api", self.api("git/ref/heads/" + branch), "--jq", ".object.sha"),
            default="<%s-sha>" % branch,
        ).strip()

    def apply_patches(self, patches, sid, state):
        for patch in patches:
            on_main = bool(patch.get("on_main"))
            branch = "main" if on_main else patch["branch"]
            if not on_main and branch not in state["branches"]:
                base = patch.get("base", "main")
                self.c.write(self.gh("api", "-X", "POST", self.api("git/refs"),
                                     "-f", "ref=refs/heads/" + branch,
                                     "-f", "sha=" + self.branch_sha(base)))
                state["branches"].append(branch)
            path = patch["path"]
            ref = "?ref=" + branch
            current = self.c.read(
                self.gh("api", "-H", "Accept: application/vnd.github.raw+json",
                        self.api("contents/" + path + ref)),
                default=patch["find"],
            )
            if patch["find"] not in current:
                raise SeedError("patch for %s on %s: text not found: %r"
                                % (path, branch, patch["find"]))
            updated = current.replace(patch["find"], patch["replace"], 1)
            file_sha = self.c.read(
                self.gh("api", self.api("contents/" + path + ref), "--jq", ".sha"),
                default="<file-sha>",
            ).strip()
            message = patch.get("message") or (
                "chore(eval): seed %s" % sid if on_main else "fix: seeded change for %s" % sid)
            content = base64.b64encode(updated.encode("utf-8")).decode("ascii")
            commit = self.c.write(
                self.gh("api", "-X", "PUT", self.api("contents/" + path),
                        "-f", "message=" + message, "-f", "content=" + content,
                        "-f", "branch=" + branch, "-f", "sha=" + file_sha,
                        "--jq", ".commit.sha"),
                default="<commit-sha>",
            ).strip()
            state["commits"].append({"branch": branch, "path": path, "sha": commit})

    def bot(self, kind, **inputs):
        """Perform an action as github-actions[bot] and wait for the workflow."""
        runs_argv = self.gh("run", "list", "--repo", self.repo, "--workflow",
                            BOT_WORKFLOW, "--limit", "1", "--json", "databaseId")
        before = self.c.json(runs_argv, [])
        before_id = before[0]["databaseId"] if before else 0
        argv = self.gh("workflow", "run", BOT_WORKFLOW, "--repo", self.repo,
                       "--ref", "main", "-f", "kind=" + kind)
        for key, value in inputs.items():
            if value not in (None, ""):
                argv += ["-f", "%s=%s" % (key, value)]
        self.c.write(argv)
        if self.c.dry_run:
            self.c.read(runs_argv, default="[]")
            self.c.read(self.gh("run", "watch", "<run-id>", "--repo", self.repo,
                                "--exit-status"))
            return
        run_id = None
        for _ in range(60):
            latest = self.c.json(runs_argv, [])
            if latest and latest[0]["databaseId"] > before_id:
                run_id = latest[0]["databaseId"]
                break
            self.c.sleep(2)
        if run_id is None:
            raise SeedError("post-as-bot run for %s never appeared" % kind)
        self.c.read(self.gh("run", "watch", str(run_id), "--repo", self.repo,
                            "--exit-status"))

    def _find_number(self, argv, match, what):
        for _ in range(5):
            items = self.c.json(argv, [{"number": self.c.fake_number(), "title": match}]
                                if self.c.dry_run else [])
            for item in items:
                if item.get("title") == match:
                    return item["number"]
            if self.c.dry_run:
                break
            self.c.sleep(2)
        raise SeedError("could not find the bot-created %s %r" % (what, match))

    def _label_flags(self, labels):
        flags = []
        for label in labels:
            flags += ["--label", label]
        return flags

    def post_comments(self, number, comments, noun):
        for comment in comments:
            if comment.get("as") == "bot":
                self.bot("issue_comment", number=str(number), body=comment["body"])
            else:
                self.c.write(self.gh(noun, "comment", str(number), "--repo", self.repo,
                                     "--body", comment["body"]))

    def create_issue(self, spec, prefix, key):
        title = "%s %s" % (prefix, spec["title"])
        body = spec.get("body", "")
        labels = spec.get("labels", [])
        assignee = spec.get("assignee")
        if spec.get("as") == "bot":
            self.bot("issue", title=title, body=body)
            number = self._find_number(
                self.gh("issue", "list", "--repo", self.repo, "--state", "open",
                        "--limit", "200", "--json", "number,title"), title, "issue")
            edit = []
            for label in labels:
                edit += ["--add-label", label]
            if assignee:
                edit += ["--add-assignee", assignee]
            if edit:
                self.c.write(self.gh("issue", "edit", str(number), "--repo", self.repo)
                             + edit)
        else:
            argv = self.gh("issue", "create", "--repo", self.repo, "--title", title,
                           "--body", body) + self._label_flags(labels)
            if assignee:
                argv += ["--assignee", assignee]
            out = self.c.write(
                argv, default="https://github.com/%s/issues/%d"
                % (self.repo, self.c.fake_number()))
            number = number_from_url(out)
        self.post_comments(number, spec.get("comments", []), "issue")
        return {"key": key, "number": number, "title": title, "as": spec.get("as", "operator")}

    def create_pr(self, spec, prefix, key):
        title = "%s %s" % (prefix, spec["title"])
        head = spec["head"]
        base = spec.get("base", "main")
        body = spec.get("body", "")
        labels = spec.get("labels", [])
        reviewers = spec.get("reviewers", [])
        is_bot = spec.get("as") == "bot"
        if is_bot:
            self.bot("pr", title=title, body=body, branch=head,
                     base=base if base != "main" else "")
            number = self._find_number(
                self.gh("pr", "list", "--repo", self.repo, "--state", "open",
                        "--head", head, "--json", "number,title"), title, "PR")
            edit = []
            for label in labels:
                edit += ["--add-label", label]
            if spec.get("assignee"):
                edit += ["--add-assignee", spec["assignee"]]
            if edit:
                self.c.write(self.gh("pr", "edit", str(number), "--repo", self.repo)
                             + edit)
            for reviewer in reviewers:
                self.bot("request_review", number=str(number), body=reviewer)
        else:
            argv = self.gh("pr", "create", "--repo", self.repo, "--head", head,
                           "--base", base, "--title", title, "--body", body)
            argv += self._label_flags(labels)
            for reviewer in reviewers:
                argv += ["--reviewer", reviewer]
            if spec.get("assignee"):
                argv += ["--assignee", spec["assignee"]]
            out = self.c.write(
                argv, default="https://github.com/%s/pull/%d"
                % (self.repo, self.c.fake_number()))
            number = number_from_url(out)
        threads = spec.get("review_threads", [])
        if threads:
            sha = self.c.read(self.gh("api", self.api("pulls/%d" % number), "--jq",
                                      ".head.sha"), default="<head-sha>").strip()
            for thread in threads:
                if thread.get("as") == "bot":
                    self.bot("review_comment", number=str(number), path=thread["path"],
                             line=str(thread["line"]), body=thread["body"])
                else:
                    self.c.write(self.gh(
                        "api", "-X", "POST", self.api("pulls/%d/comments" % number),
                        "-f", "body=" + thread["body"], "-f", "commit_id=" + sha,
                        "-f", "path=" + thread["path"], "-F", "line=%d" % thread["line"],
                        "-f", "side=RIGHT"))
        self.post_comments(number, spec.get("comments", []), "pr")
        for review in spec.get("reviews", []):
            state = review.get("state", "comment")
            if review.get("as") == "bot":
                self.bot("review", number=str(number), body=review.get("body", ""),
                         state=state)
            else:
                flag = {"approve": "--approve", "request_changes": "--request-changes"}.get(
                    state, "--comment")
                self.c.write(self.gh("pr", "review", str(number), "--repo", self.repo,
                                     flag, "--body", review.get("body", "")))
        return {"key": key, "number": number, "title": title, "head": head,
                "as": spec.get("as", "operator")}

    def seed(self, spec, rid, sid):
        """Apply one repo's share of a scenario; return what was created."""
        prefix = "[eval:%s:%s]" % (rid, sid)
        operator = self.c.read(self.gh("api", "user", "--jq", ".login"),
                               default="<operator>").strip()
        spec = subst(spec, {"rid": rid, "id": sid, "operator": operator})
        state = {"branches": [], "commits": []}
        self.apply_patches(spec.get("patches", []), sid, state)
        created = {"issues": [], "prs": []}
        for index, issue in enumerate(spec.get("issues", [])):
            created["issues"].append(
                self.create_issue(issue, prefix, issue.get("key", "issue%d" % (index + 1))))
        for index, pr in enumerate(spec.get("prs", [])):
            created["prs"].append(
                self.create_pr(pr, prefix, pr.get("key", "pr%d" % (index + 1))))
        created["branches"] = state["branches"]
        created["commits"] = state["commits"]
        return created


# --------------------------------------------------------------------------
# drivers


def write_manifest(state_dir, rid, sid, manifest):
    path = Path(state_dir) / rid / ("%s.json" % sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def run_scenario(main_fx, companion_fx, rid, sid, state_dir):
    spec = load_scenario(sid)
    client = main_fx.c
    main_fx.acquire(rid)
    started = client.now().strftime(TS_FORMAT)
    created = main_fx.seed(spec, rid, sid)
    companion_created = None
    if spec.get("companion"):
        if companion_fx is None:
            raise SeedError("scenario %s needs --companion" % sid)
        if any(i.get("as") == "bot" for i in spec["companion"].get("issues", [])) or any(
                p.get("as") == "bot" for p in spec["companion"].get("prs", [])):
            raise SeedError("the companion repo has no post-as-bot workflow")
        companion_created = companion_fx.seed(spec["companion"], rid, sid)
    snapshot = main_fx.snapshot()
    manifest = {
        "run_id": rid,
        "scenario": sid,
        "repo": main_fx.repo,
        "companion": companion_fx.repo if companion_fx else None,
        "started_at": started,
        "baseline_sha": main_fx.baseline_sha(),
        "shas": {"branches": snapshot["branches"],
                 "main": snapshot["branches"].get("main"),
                 "develop": snapshot["branches"].get("develop")},
        "created": created,
        "companion_created": companion_created,
        "snapshot": snapshot,
        "companion_snapshot": companion_fx.snapshot() if companion_fx else None,
        "expect": spec.get("expect", {}),
        "writes": list(client.writes),
    }
    if client.dry_run:
        print("+ (would write %s)" % (Path(state_dir) / rid / ("%s.json" % sid)),
              file=client.out)
        return manifest
    path = write_manifest(state_dir, rid, sid, manifest)
    print("manifest: %s" % path, file=client.out)
    return manifest


def build_parser():
    p = argparse.ArgumentParser(description="Seed and reset the not-me eval fixture repos.")
    p.add_argument("--repo", default=os.environ.get("NOT_ME_FIXTURE_REPO"),
                   help="owner/name of the fixture repo (env NOT_ME_FIXTURE_REPO)")
    p.add_argument("--companion", default=os.environ.get("NOT_ME_FIXTURE_COMPANION"),
                   help="owner/name of the companion repo (env NOT_ME_FIXTURE_COMPANION)")
    p.add_argument("--owner", default=os.environ.get("NOT_ME_FIXTURE_OWNER"),
                   help="the only owner this tool may touch (env NOT_ME_FIXTURE_OWNER)")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--bootstrap", action="store_true")
    mode.add_argument("--reset", action="store_true")
    mode.add_argument("--scenario", metavar="ID")
    mode.add_argument("--teardown", metavar="RID")
    mode.add_argument("--verify", action="store_true")
    p.add_argument("--run-id", metavar="RID")
    p.add_argument("--manifest", metavar="PATH", help="with --verify: compare to this manifest")
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv=None, run=default_run, out=None, err=None, now=utcnow,
         sleep=time.sleep, state_dir=None):
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    args = build_parser().parse_args(argv)
    state_dir = state_dir or (ROOT / "evals" / ".state")
    client = Client(run=run, dry_run=args.dry_run, out=out, err=err, sleep=sleep, now=now)
    try:
        if not args.repo:
            raise SafetyError("--repo is required (or NOT_ME_FIXTURE_REPO)")
        check_slug(args.repo, args.owner)
        main_fx = Fixture(client, args.repo)
        companion_fx = None
        if args.companion:
            check_slug(args.companion, args.owner)
            companion_fx = Fixture(client, args.companion)
        for fx in [main_fx] + ([companion_fx] if companion_fx else []):
            fx.check_private()

        if args.bootstrap:
            main_fx.bootstrap(HERE / "repo")
            if companion_fx:
                companion_fx.bootstrap(HERE / "companion")
        elif args.reset:
            rid = args.run_id or "reset"
            main_fx.acquire(rid)
            main_fx.reset()
            if companion_fx:
                companion_fx.reset()
            if not args.run_id:
                main_fx.release(rid)
        elif args.scenario:
            if not args.run_id:
                raise SeedError("--scenario needs --run-id")
            run_scenario(main_fx, companion_fx, args.run_id, args.scenario, state_dir)
        elif args.teardown:
            rid = args.teardown
            for lock in main_fx._locks():
                holder, ts = main_fx.parse_lock(lock.get("body"))
                if holder != rid and holder and not main_fx._stale(ts):
                    raise LockError("eval-lock is held by %s, not %s" % (holder, rid))
            main_fx.reset()
            if companion_fx:
                companion_fx.reset()
            main_fx.release(rid)
        elif args.verify:
            sha = main_fx.baseline_sha()
            if args.manifest:
                with open(args.manifest, encoding="utf-8") as fh:
                    expected = json.load(fh)["snapshot"]
            else:
                expected = main_fx.baseline_snapshot(sha)
            diffs = diff_snapshots(expected, main_fx.snapshot())
            if diffs and not client.dry_run:
                print("\n".join(diffs), file=out)
                return 1
            print("verify: no differences", file=out)
        return 0
    except (SafetyError, LockError) as exc:
        print("refused: %s" % exc, file=err)
        return 2
    except SeedError as exc:
        print("error: %s" % exc, file=err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
