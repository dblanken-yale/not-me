#!/usr/bin/env python3
"""Stage the workspace and sandbox home for one live eval case.

`claude plugin eval` runs a case's scaffold with only PATH, HOME (the sandbox home), TMPDIR and TERM set,
so nothing the harness exports reaches it: no run id, no manifest path, no token. The work is split in two:

    scaffold.py prepare --manifest PATH --stage-dir DIR
                                   operator side, before `claude plugin eval`. Needs NOT_ME_FIXTURE_TOKEN, a
                                   dedicated fine-grained token, and refuses without it. The token goes to
                                   gh only as GH_TOKEN in a scrubbed environment, never on a command line. It checks that the
                                   token sees the fixture repo and cannot see this plugin repo, clones the
                                   fixture repo (and the companion) at the manifest's SHAs into DIR, stages
                                   the token there (mode 600) and points evals/.state/current-stage.json at
                                   it. The operator's own gh login is never read.
    scaffold.py stage --case NAME  inside the eval, from the case's scaffold.sh. Copies the staged clones
                                   into the working directory, writes the scenario's config layers, the
                                   pause file and the allow list, signs gh in for the sandbox home and runs
                                   `gh auth setup-git`.
    scaffold.py clean [--stage-dir DIR]
                                   operator side, after the run. Removes the stage and its token copy.

Stdlib only. See plugins/not-me/evals/README.md for the contract and the scenario `config` block.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PLUGIN = ROOT / "plugins" / "not-me"
SCENARIOS = ROOT / "fixture" / "scenarios"
STATE = ROOT / "evals" / ".state"
POINTER = "current-stage.json"
COMPANION_DIR = ".not-me-companion"
EVAL_DIR = ".not-me-eval"
EXCLUDES = ("/.claude/not-me/", "/" + COMPANION_DIR + "/", "/" + EVAL_DIR + "/")
DOCS = ("project.md", "standards.md", "verify.md", "review.md", "prove.md")
CONFIG_KEYS = {"none", "global", "repo", "pause", "allow_companions", "files", "git_files", "commit_repo_layer"}
CASE_ID = re.compile(r"(?:^|-)([A-Z]\d+)$")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


TOKEN_RE = re.compile(r"(?:github_pat|gh[pousr])_[A-Za-z0-9_]+")


def redact(text: Any) -> str:
    """Hide anything that looks like a GitHub token."""
    return TOKEN_RE.sub("<redacted-token>", str(text))


class ScaffoldError(Exception):
    """An error whose message never carries a token."""

    def __init__(self, message: Any = ""):
        super().__init__(redact(message))


def run(argv: Sequence[str], cwd: Optional[Path] = None, env: Optional[Mapping[str, str]] = None,
        input_text: Optional[str] = None) -> "subprocess.CompletedProcess[str]":
    try:
        proc = subprocess.run(list(argv), cwd=str(cwd) if cwd else None, env=dict(env) if env is not None else None,
                              input=input_text, capture_output=True, text=True)
    except OSError as exc:
        raise ScaffoldError(f"{argv[0]} could not run: {exc}")
    if proc.returncode != 0:
        raise ScaffoldError(f"{' '.join(argv[:3])} failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
    return proc


# --------------------------------------------------------------------------
# the scenario `config` block


def scenario_id(case: str) -> str:
    found = CASE_ID.search(case)
    if not found:
        raise ScaffoldError(f"case {case!r} does not end in a scenario id like N1")
    return found.group(1)


def load_scenario(sid: str, scenarios: Path = SCENARIOS) -> Dict[str, Any]:
    path = scenarios / f"{sid}.json"
    if not path.is_file():
        raise ScaffoldError(f"no scenario file {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def check_config(config: Any) -> List[str]:
    """Problems with a scenario's `config` block (empty when it is well formed)."""
    if config is None:
        return []
    if not isinstance(config, dict):
        return ["config must be an object"]
    problems = [f"unknown config key {k!r}" for k in sorted(set(config) - CONFIG_KEYS)]
    for layer in ("global", "repo"):
        docs = config.get(layer, {})
        if not isinstance(docs, dict) or any(k not in DOCS or not isinstance(v, str) for k, v in docs.items()):
            problems.append(f"config.{layer} must map one of {', '.join(DOCS)} to the file text")
    if config.get("pause") not in (None, "global", "project"):
        problems.append("config.pause must be global or project")
    for key in ("files", "git_files"):
        files = config.get(key, {})
        if not isinstance(files, dict) or not all(isinstance(v, str) for v in files.values()):
            problems.append(f"config.{key} must map relative paths to text")
            continue
        for rel in files:
            p = Path(rel)
            if p.is_absolute() or ".." in p.parts or not rel:
                problems.append(f"config.{key} path {rel!r} must be relative and stay inside")
    if config.get("none") and (config.get("global") or config.get("repo")):
        problems.append("config.none cannot be combined with global or repo docs")
    return problems


def substitute(text: str, values: Mapping[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def with_mode(text: Optional[str], mode: str) -> str:
    """The global project.md with `mode:` set, unless the scenario's own text already sets it."""
    if not text:
        return f"---\nschema: 1\nmode: {mode}\nextends: default\n---\n"
    if not text.startswith("---\n"):
        return f"---\nschema: 1\nmode: {mode}\nextends: default\n---\n" + text
    head, sep, rest = text[4:].partition("\n---")
    if re.search(r"(?m)^mode:", head):
        return text
    return "---\nmode: " + mode + "\n" + head + sep + rest


def layer_files(config: Optional[Mapping[str, Any]], backend: str, values: Mapping[str, str]) -> Dict[str, Dict[str, str]]:
    """{"global": {name: text}, "repo": {name: text}} for a scenario config, placeholders filled.

    Every scenario gets `mode: live` (github backend) or `mode: dry-run` (ledger) in the global project.md,
    except one with `none`, which gets no layer at all: a fresh install, which is dry-run by default.
    """
    config = dict(config or {})
    if config.get("none"):
        return {"global": {}, "repo": {}}
    mode = "dry-run" if backend == "ledger" else "live"
    glob = {k: substitute(v, values) for k, v in (config.get("global") or {}).items()}
    glob["project.md"] = with_mode(glob.get("project.md"), mode)
    repo = {k: substitute(v, values) for k, v in (config.get("repo") or {}).items()}
    return {"global": glob, "repo": repo}


# --------------------------------------------------------------------------
# prepare (operator side)

TOKEN_ENV = "NOT_ME_FIXTURE_TOKEN"
PLUGIN_REPO_ENV = "NOT_ME_PLUGIN_REPO"


def _clone(repo: str, sha: Optional[str], dest: Path, runner: Runner, env: Mapping[str, str]) -> None:
    runner(["gh", "repo", "clone", repo, str(dest), "--", "--quiet"], env=env)
    runner(["git", "-C", str(dest), "remote", "set-url", "origin", f"https://github.com/{repo}.git"])
    if sha:
        runner(["git", "-C", str(dest), "checkout", "-q", "-B", "main", sha])


def plugin_repo(env: Mapping[str, str], runner: Runner) -> str:
    """owner/name of this plugin repo: NOT_ME_PLUGIN_REPO, else the origin remote of this checkout."""
    if env.get(PLUGIN_REPO_ENV):
        return env[PLUGIN_REPO_ENV]
    url = runner(["git", "-C", str(ROOT), "remote", "get-url", "origin"]).stdout.strip()
    found = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", url)
    if not found:
        raise ScaffoldError(f"cannot tell which repo is this plugin repo from {url!r}; set {PLUGIN_REPO_ENV}")
    return found.group(1)


def check_token(token: str, repos: Sequence[str], plugin: str, runner: Runner, env: Mapping[str, str]) -> Dict[str, Any]:
    """Refuse anything but a fine-grained token that can push to the fixture repos and has no write access
    to the plugin repo: an explicit HTTP 404 (private plugin repo) or a 200 whose permissions.push and
    permissions.admin are both false (public plugin repo). Any other failure on the plugin repo (network,
    401, 403, 5xx, gh missing) also refuses, because it proves nothing about access. Returns the user."""
    if not token.startswith("github_pat_"):
        kind = "a classic or OAuth token" if token.startswith(("ghp_", "gho_", "ghu_", "ghs_")) else \
            "not a fine-grained token"
        raise ScaffoldError(f"{TOKEN_ENV} is {kind}. It must be a fine-grained token (github_pat_...) limited "
                            "to the fixture repos (see docs/testing.md)")
    # The token reaches gh only through GH_TOKEN in `env`, never in argv (argv shows in the process list).
    try:
        user = json.loads(runner(["gh", "api", "user"], env=env).stdout)
    except (ScaffoldError, ValueError) as exc:
        raise ScaffoldError(f"{TOKEN_ENV} was rejected by GitHub: {exc}")
    for repo in repos:
        try:
            info = json.loads(runner(["gh", "api", f"repos/{repo}"], env=env).stdout)
        except (ScaffoldError, ValueError) as exc:
            raise ScaffoldError(f"{TOKEN_ENV} cannot read the fixture repo {repo}: {exc}")
        if not (info.get("permissions") or {}).get("push") is True:
            raise ScaffoldError(f"{TOKEN_ENV} cannot push to the fixture repo {repo} (permissions.push is not true)")
    try:
        raw = runner(["gh", "api", f"repos/{plugin}"], env=env).stdout
    except ScaffoldError as exc:
        if re.search(r"HTTP 404\b", str(exc)):
            return user
        raise ScaffoldError(f"cannot confirm {TOKEN_ENV} has no access to {plugin}: expected HTTP 404 or a "
                            f"200 without push, got: {exc}")
    try:
        perms = json.loads(raw).get("permissions") or {}
    except (ValueError, AttributeError):
        raise ScaffoldError(f"cannot confirm {TOKEN_ENV} has no access to {plugin}: unreadable reply")
    if not isinstance(perms, dict):
        raise ScaffoldError(f"cannot confirm {TOKEN_ENV} has no access to {plugin}: unreadable reply")
    # A public plugin repo answers 200 to every token; what matters is that this one cannot write to it.
    if perms.get("push") is False and perms.get("admin") is False:
        return user
    raise ScaffoldError(f"{TOKEN_ENV} can write to {plugin}, the plugin repo. Refusing: it must be a fine-grained "
                        "token limited to the fixture repos (see docs/testing.md)")


def prepare(manifest_path: Any, stage: Path, env: Mapping[str, str], runner: Runner = run,
            state: Path = STATE, backend: Optional[str] = None) -> Path:
    """Clone the fixture at the manifest's SHAs into `stage` and stage the dedicated fixture token.

    The only token used is NOT_ME_FIXTURE_TOKEN. The operator's own gh login is never read, and gh is run
    with an empty config dir so it cannot fall back to it.
    """
    token = (env.get(TOKEN_ENV) or "").strip()
    if not token:
        raise ScaffoldError(f"{TOKEN_ENV} is not set. Create a fine-grained token limited to the two fixture "
                            "repos (docs/testing.md) and export it; the operator's gh login is never used")
    if not manifest_path or not Path(manifest_path).is_file():
        raise ScaffoldError("--manifest must name the manifest the seeder wrote")
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    sid, rid = manifest["scenario"], manifest["run_id"]
    spec = load_scenario(sid)
    problems = check_config(spec.get("config"))
    if problems:
        raise ScaffoldError(f"scenario {sid}: " + "; ".join(problems))
    companion = manifest.get("companion")
    with_companion = bool(companion and spec.get("companion") is not None)

    empty_cfg = Path(tempfile.mkdtemp(prefix="not-me-gh-"))
    gh_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(empty_cfg), "GH_CONFIG_DIR": str(empty_cfg),
              "GH_TOKEN": token, "GH_PROMPT_DISABLED": "1"}
    try:
        user = check_token(token, [manifest["repo"]] + ([companion] if with_companion else []),
                           plugin_repo(env, runner), runner, gh_env)
        if stage.exists():
            shutil.rmtree(str(stage))
        stage.mkdir(parents=True)
        _clone(manifest["repo"], (manifest.get("shas") or {}).get("main"), stage / "repo", runner, gh_env)
        if with_companion:
            csha = ((manifest.get("companion_snapshot") or {}).get("branches") or {}).get("main")
            _clone(companion, csha, stage / "companion", runner, gh_env)
    finally:
        shutil.rmtree(str(empty_cfg), ignore_errors=True)
    token_file = stage / "gh-token"
    fd = os.open(str(token_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    os.chmod(str(token_file), 0o600)
    info = {"run_id": rid, "scenario": sid, "backend": backend or env.get("NOT_ME_EVAL_BACKEND") or "github",
            "repo": manifest["repo"], "companion": companion if with_companion else None,
            "owner": env.get("NOT_ME_FIXTURE_OWNER") or manifest["repo"].split("/")[0],
            "operator": user["login"], "operator_id": str(user.get("id", "")),
            "prepared_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    (stage / "stage.json").write_text(json.dumps(info, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        where = stage.resolve().relative_to(state.resolve()).as_posix()
    except ValueError:
        where = str(stage.resolve())
    state.mkdir(parents=True, exist_ok=True)
    (state / POINTER).write_text(json.dumps({"run_id": rid, "scenario": sid, "stage": where}) + "\n",
                                 encoding="utf-8")
    return stage


def clean(state: Path = STATE, stage_dir: Optional[Path] = None) -> List[Path]:
    """Remove the stage (token copy included) and the pointer. Safe to call when nothing was prepared."""
    pointer = state / POINTER
    removed: List[Path] = []
    stages = [Path(stage_dir)] if stage_dir else []
    if pointer.is_file():
        try:
            stages.append(state / json.loads(pointer.read_text(encoding="utf-8"))["stage"])
        except (ValueError, KeyError):
            pass
    for stage in stages:
        if stage.exists():
            shutil.rmtree(str(stage))
            removed.append(stage)
    if pointer.is_file():
        pointer.unlink()
        removed.append(pointer)
    return removed


# --------------------------------------------------------------------------
# stage (inside the eval)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _hosts_yml(login: str, token: str) -> str:
    return (f"github.com:\n    users:\n        {login}:\n            oauth_token: {token}\n"
            f"    git_protocol: https\n    oauth_token: {token}\n    user: {login}\n")


def _identity(repo_dir: Path, info: Mapping[str, str], runner: Runner) -> None:
    login = info["operator"]
    email = f"{info['operator_id']}+{login}@users.noreply.github.com" if info.get("operator_id") else \
        f"{login}@users.noreply.github.com"
    runner(["git", "-C", str(repo_dir), "config", "user.name", login])
    runner(["git", "-C", str(repo_dir), "config", "user.email", email])


def stage_workspace(case: str, workspace: Path, home: Path, state: Path = STATE, runner: Runner = run,
                    scenarios: Path = SCENARIOS, login_gh: bool = True) -> Dict[str, Any]:
    """Fill `workspace` and `home` for one case run. Returns what was written (no secrets)."""
    sid = scenario_id(case)
    pointer = state / POINTER
    if not pointer.is_file():
        raise ScaffoldError("no prepared stage: run `scaffold.py prepare` with the run's environment first")
    ptr = json.loads(pointer.read_text(encoding="utf-8"))
    if ptr.get("scenario") != sid:
        raise ScaffoldError(f"the prepared stage is for scenario {ptr.get('scenario')}, this case needs {sid}")
    stage = state / ptr["stage"]
    info = json.loads((stage / "stage.json").read_text(encoding="utf-8"))
    spec = load_scenario(sid, scenarios)
    config = spec.get("config") or {}
    problems = check_config(config)
    if problems:
        raise ScaffoldError(f"scenario {sid}: " + "; ".join(problems))

    workspace.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(stage / "repo"), str(workspace), symlinks=True, dirs_exist_ok=True)
    git_dir = workspace / ".git"
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with open(str(exclude), "a", encoding="utf-8") as fh:
        fh.write("\n# not-me eval scaffold\n" + "\n".join(EXCLUDES) + "\n")
    _identity(workspace, info, runner)

    companion = info.get("companion")
    companion_name = companion.split("/")[1] if companion else ""
    if companion:
        dest = workspace / COMPANION_DIR / companion_name
        shutil.copytree(str(stage / "companion"), str(dest), symlinks=True)
        _identity(dest, info, runner)

    values = {"repo": info["repo"], "companion": companion or "", "companion_name": companion_name,
              "companion_path": f"{COMPANION_DIR}/{companion_name}" if companion else "",
              "operator": info["operator"], "owner": info["owner"]}
    layers = layer_files(config, info["backend"], values)
    global_dir = home / ".claude" / "not-me"
    written: List[str] = []
    for name, text in layers["global"].items():
        _write(global_dir / name, text)
        written.append(f"~/.claude/not-me/{name}")
    for name, text in layers["repo"].items():
        _write(workspace / ".claude" / "not-me" / name, text)
        written.append(f".claude/not-me/{name}")
    for rel, text in (config.get("files") or {}).items():
        _write(workspace / rel, substitute(text, values))
        written.append(rel)
    for rel, text in (config.get("git_files") or {}).items():
        _write(git_dir / rel, substitute(text, values))
        written.append(f".git/{rel}")
    if config.get("pause") == "global":
        _write(global_dir / "PAUSED", "eval\n")
        written.append("~/.claude/not-me/PAUSED")
    elif config.get("pause") == "project":
        _write(git_dir / "not-me" / "PAUSED", "eval\n")
        written.append(".git/not-me/PAUSED")
    if config.get("allow_companions") and companion:
        sys.path.insert(0, str(PLUGIN))
        from notme import writes  # noqa: E402  (the plugin's own allow-list writer keeps the format in sync)
        writes.allowlist_add(global_dir, info["repo"], repo=companion)
        written.append("~/.claude/not-me/allowed-repos.json")
    if config.get("commit_repo_layer") and layers["repo"]:
        runner(["git", "-C", str(workspace), "add", "-f", ".claude/not-me"])
        runner(["git", "-C", str(workspace), "commit", "-q", "-m", "chore(eval): repo layer config"])

    if login_gh:
        token = (stage / "gh-token").read_text(encoding="utf-8").strip()
        hosts = home / ".config" / "gh" / "hosts.yml"
        _write(hosts, _hosts_yml(info["operator"], token))
        os.chmod(str(hosts), 0o600)
        gh_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
                  "XDG_CONFIG_HOME": str(home / ".config"), "GH_PROMPT_DISABLED": "1"}
        runner(["gh", "auth", "setup-git", "--hostname", "github.com"], env=gh_env)

    record = {"run_id": info["run_id"], "scenario": sid, "case": case, "backend": info["backend"],
              "repo": info["repo"], "companion": companion, "written": written,
              "env": {"NOT_ME_EVAL_RUN_ID": info["run_id"], "NOT_ME_EVAL_SCENARIO": sid,
                      "NOT_ME_EVAL_BACKEND": info["backend"], "NOT_ME_FIXTURE_REPO": info["repo"],
                      "NOT_ME_FIXTURE_COMPANION": companion or "", "NOT_ME_FIXTURE_OWNER": info["owner"]}}
    _write(home / ".not-me-eval.json", json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record


# --------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("prepare", help="clone the fixture at the manifest's SHAs into a stage (operator side)")
    pr.add_argument("--manifest", required=True)
    pr.add_argument("--stage-dir", required=True)
    pr.add_argument("--backend", default=None, choices=("github", "ledger"))
    st = sub.add_parser("stage", help="fill the eval workspace and sandbox home (run by a case's scaffold.sh)")
    st.add_argument("--case", required=True)
    st.add_argument("--workspace", default=".", help="default: the current directory")
    cl = sub.add_parser("clean", help="remove the prepared stage and its token copy")
    cl.add_argument("--stage-dir", default=None)
    sub.add_parser("check", help="validate every scenario's config block")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "prepare":
            print(f"stage: {prepare(args.manifest, Path(args.stage_dir), os.environ, backend=args.backend)}")
        elif args.cmd == "stage":
            home = Path(os.environ.get("HOME") or Path.home())
            record = stage_workspace(args.case, Path(args.workspace).resolve(), home)
            print(json.dumps(record, sort_keys=True))
        elif args.cmd == "clean":
            for path in clean(stage_dir=Path(args.stage_dir) if args.stage_dir else None):
                print(f"removed {path}")
        else:
            bad = 0
            for path in sorted(SCENARIOS.glob("*.json")):
                for problem in check_config(json.loads(path.read_text(encoding="utf-8")).get("config")):
                    print(f"{path.name}: {problem}")
                    bad += 1
            return 1 if bad else 0
    except (ScaffoldError, OSError, ValueError, KeyError) as exc:
        print(f"scaffold: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
