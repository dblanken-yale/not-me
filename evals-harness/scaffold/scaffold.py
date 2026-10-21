#!/usr/bin/env python3
"""Stage the workspace and sandbox home for one live eval case.

`claude plugin eval` runs a case's scaffold with only PATH, HOME (the sandbox home), TMPDIR and TERM set,
so nothing the harness exports reaches it: no run id, no manifest path, no token. The work is split in two:

    scaffold.py prepare            operator side, before `claude plugin eval`. Reads NOT_ME_EVAL_MANIFEST,
                                   NOT_ME_EVAL_BACKEND and GH_TOKEN (else `gh auth token`), clones the
                                   fixture repo (and the companion) at the manifest's SHAs into
                                   evals/.state/<RID>/<ID>.stage/ and points evals/.state/current-stage.json
                                   at it.
    scaffold.py stage --case NAME  inside the eval, from the case's scaffold.sh. Copies the staged clones
                                   into the working directory, writes the scenario's config layers, the
                                   pause file and the allow list, signs gh in for the sandbox home and runs
                                   `gh auth setup-git`.
    scaffold.py clean              operator side, after the run. Removes the stage (and its token copy).

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


class ScaffoldError(Exception):
    pass


def run(argv: Sequence[str], cwd: Optional[Path] = None, env: Optional[Mapping[str, str]] = None,
        input_text: Optional[str] = None) -> "subprocess.CompletedProcess[str]":
    proc = subprocess.run(list(argv), cwd=str(cwd) if cwd else None, env=dict(env) if env is not None else None,
                          input=input_text, capture_output=True, text=True)
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


def _clone(repo: str, sha: Optional[str], dest: Path, runner: Runner) -> None:
    runner(["gh", "repo", "clone", repo, str(dest), "--", "--quiet"])
    runner(["git", "-C", str(dest), "remote", "set-url", "origin", f"https://github.com/{repo}.git"])
    if sha:
        runner(["git", "-C", str(dest), "checkout", "-q", "-B", "main", sha])


def prepare(env: Mapping[str, str], runner: Runner = run, state: Path = STATE) -> Path:
    manifest_path = env.get("NOT_ME_EVAL_MANIFEST")
    if not manifest_path or not Path(manifest_path).is_file():
        raise ScaffoldError("NOT_ME_EVAL_MANIFEST must name the manifest the seeder wrote")
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    sid, rid = manifest["scenario"], manifest["run_id"]
    spec = load_scenario(sid)
    problems = check_config(spec.get("config"))
    if problems:
        raise ScaffoldError(f"scenario {sid}: " + "; ".join(problems))
    backend = env.get("NOT_ME_EVAL_BACKEND") or "github"
    stage = state / rid / f"{sid}.stage"
    if stage.exists():
        shutil.rmtree(str(stage))
    stage.mkdir(parents=True)
    _clone(manifest["repo"], (manifest.get("shas") or {}).get("main"), stage / "repo", runner)
    companion = manifest.get("companion")
    if companion and spec.get("companion") is not None:
        csha = ((manifest.get("companion_snapshot") or {}).get("branches") or {}).get("main")
        _clone(companion, csha, stage / "companion", runner)
    token = env.get("GH_TOKEN") or runner(["gh", "auth", "token"]).stdout.strip()
    user = json.loads(runner(["gh", "api", "user"]).stdout)
    token_file = stage / "gh-token"
    token_file.write_text(token + "\n", encoding="utf-8")
    os.chmod(str(token_file), 0o600)
    info = {"run_id": rid, "scenario": sid, "backend": backend, "repo": manifest["repo"],
            "companion": companion if (stage / "companion").is_dir() else None,
            "owner": env.get("NOT_ME_FIXTURE_OWNER") or manifest["repo"].split("/")[0],
            "operator": user["login"], "operator_id": str(user.get("id", "")),
            "prepared_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    (stage / "stage.json").write_text(json.dumps(info, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (state / POINTER).write_text(json.dumps({"run_id": rid, "scenario": sid,
                                             "stage": stage.relative_to(state).as_posix()}) + "\n", encoding="utf-8")
    return stage


def clean(state: Path = STATE) -> List[Path]:
    pointer = state / POINTER
    removed: List[Path] = []
    if pointer.is_file():
        stage = state / json.loads(pointer.read_text(encoding="utf-8"))["stage"]
        if stage.exists():
            shutil.rmtree(str(stage))
            removed.append(stage)
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
    sub.add_parser("prepare", help="clone the fixture at the manifest's SHAs into a stage (operator side)")
    st = sub.add_parser("stage", help="fill the eval workspace and sandbox home (run by a case's scaffold.sh)")
    st.add_argument("--case", required=True)
    st.add_argument("--workspace", default=".", help="default: the current directory")
    sub.add_parser("clean", help="remove the prepared stage and its token copy")
    sub.add_parser("check", help="validate every scenario's config block")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "prepare":
            print(f"stage: {prepare(os.environ)}")
        elif args.cmd == "stage":
            home = Path(os.environ.get("HOME") or Path.home())
            record = stage_workspace(args.case, Path(args.workspace).resolve(), home)
            print(json.dumps(record, sort_keys=True))
        elif args.cmd == "clean":
            for path in clean():
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
