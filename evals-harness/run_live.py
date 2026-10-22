#!/usr/bin/env python3
"""Run one live eval scenario end to end: seed, run the skill in a real Claude Code session, grade, tear down.

    python3 evals-harness/run_live.py --skill new --scenario N1 [--runs N] [--backend github|ledger]
        [--launch print|interactive] [--repo O/N] [--companion O/N] [--owner O] [--out DIR]
        [--keep-temp] [--dry-run]
    python3 evals-harness/run_live.py --restore-global RUN_ID

Per run: seed_fixture.py --reset and --scenario, scaffold.py prepare, stage the workspace (the scenario's
global config layer goes into your real ~/.claude/not-me, which is backed up first and restored after),
launch `claude` in the workspace with your own login, grade the transcript and the GitHub (or ledger) state,
then clean up and seed_fixture.py --teardown (also when a step failed). One result file per run:
<out>/<ID>-<n>.json. Runs are sequential on purpose: a skill scans the whole fixture repo.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import graders  # noqa: E402
import transcript as transcript_mod  # noqa: E402

SEED = ROOT / "fixture" / "seed_fixture.py"
SCAFFOLD = HERE / "scaffold" / "scaffold.py"
GRADE = HERE / "grade_state.py"
PLUGIN_DIR = ROOT / "plugins" / "not-me"
STATE_DIR = ROOT / "evals" / ".state"
CASES_DIR = graders.CASES
BACKUP_RECORD = "global-backup.json"
DEFAULT_TIMEOUT = 1800
# Credentials the session must not see: it uses the interactive Claude login and the fixture GH_TOKEN only.
STRIPPED_ENV = ("NOT_ME_FIXTURE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN")

# runner(argv, env, **opts): opts are cwd, stdout_path (capture stdout to a file), stdin_devnull and timeout.
Runner = Callable[..., int]


def subprocess_runner(argv: Sequence[str], env: Dict[str, str], cwd: Optional[Path] = None,
                      stdout_path: Optional[Path] = None, stdin_devnull: bool = False,
                      timeout: Optional[float] = None) -> int:
    kwargs: Dict[str, Any] = {"env": env, "cwd": str(cwd or ROOT), "timeout": timeout}
    if stdin_devnull:
        kwargs["stdin"] = subprocess.DEVNULL
    try:
        if stdout_path is not None:
            with open(str(stdout_path), "wb") as fh:
                return subprocess.run(list(argv), stdout=fh, **kwargs).returncode
        return subprocess.run(list(argv), **kwargs).returncode
    except subprocess.TimeoutExpired:
        return 124


TOKEN_RE = re.compile(r"(?:github_pat|gh[pousr])_[A-Za-z0-9_]+")


def redact(text: str) -> str:
    """Hide GitHub tokens, and the exact NOT_ME_FIXTURE_TOKEN value, in anything printed."""
    secret = os.environ.get("NOT_ME_FIXTURE_TOKEN")
    if secret:
        text = text.replace(secret, "<redacted-token>")
    return TOKEN_RE.sub("<redacted-token>", text)


def quote(argv: Sequence[str]) -> str:
    return redact(" ".join(shlex.quote(str(a)) for a in argv))


def new_run_id(scenario: str, n: int) -> str:
    return f"{scenario.lower()}-{n}-{time.strftime('%y%m%d%H%M%S')}-{uuid.uuid4().hex[:4]}"


def read_session_json(path: Path) -> Dict[str, Any]:
    """The result object of `claude -p --output-format json` ({} when the file is missing or not JSON)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(data, list):  # an event stream: the last result event
        data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), {})
    return data if isinstance(data, dict) else {}


def global_dir(home: Path) -> Path:
    return home / ".claude" / "not-me"


def backup_global(home: Path, record_path: Path) -> None:
    """Move the operator's ~/.claude/not-me aside and record where it went. The record is written first, so a
    crash at any point leaves enough to put everything back with --restore-global."""
    gdir = global_dir(home)
    backup = record_path.parent / "global-backup"
    if backup.exists():
        raise RuntimeError(f"{backup} already exists; run --restore-global first")
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record = {"original": str(gdir.absolute()), "backup": str(backup.absolute()), "had_original": gdir.exists(),
              "run_id": record_path.parent.name,
              "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if gdir.exists():
        shutil.move(str(gdir), str(backup))


def _force_remove(path: Path) -> None:
    def onerror(func: Callable[..., Any], target: str, _exc: Any) -> None:
        os.chmod(target, stat.S_IWRITE)  # read-only git objects on Windows
        func(target)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(str(path), onerror=onerror)
    elif path.exists() or path.is_symlink():
        path.unlink()


def restore_global(record_path: Path) -> str:
    """Undo backup_global: remove what the run wrote, move the backup back, then drop the record.
    The backup is never deleted before it is back in place."""
    record = json.loads(record_path.read_text(encoding="utf-8"))
    original, backup = Path(record["original"]), Path(record["backup"])
    if backup.exists():
        _force_remove(original)
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(backup), str(original))
        what = f"restored {original}"
    elif record.get("had_original"):
        what = f"{original} was never moved; left as it is"  # crashed before the move
    else:
        _force_remove(original)
        what = f"removed the layer the run wrote at {original} (there was none before)"
    record_path.unlink()
    return what


class Plan:
    """The commands for one run, built once so --dry-run and a real run cannot drift apart."""

    def __init__(self, args: argparse.Namespace, rid: str, n: int, out: Path, case: "graders.Case", home: Path):
        self.rid, self.n, self.out, self.case, self.home = rid, n, out, case, home
        flags: List[str] = ["--repo", args.repo, "--owner", args.owner]
        if args.companion:
            flags += ["--companion", args.companion]
        py = sys.executable or "python3"
        sid = args.scenario
        run_dir = STATE_DIR / rid
        self.manifest = run_dir / f"{sid}.json"
        self.stage_dir = run_dir / f"{sid}.stage"
        self.workspace = run_dir / f"{sid}.workspace"
        self.gh_config = self.stage_dir / "gh-config"
        self.token_file = self.stage_dir / "gh-token"
        self.backup_record = run_dir / BACKUP_RECORD
        self.backup_dir = run_dir / "global-backup"
        self.session_json = out / f"{sid}-{n}.session.json"
        self.graders_json = out / f"{sid}-{n}.graders.json"
        self.grade_json = out / f"{sid}-{n}.grade.json"
        self.result = out / f"{sid}-{n}.json"
        self.ledger = Path(args.ledger) if args.ledger else self.workspace / ".git" / "not-me" / "logs" / "intended-writes.jsonl"
        self.timeout = case.meta.get("timeout_seconds")
        self.timeout = self.timeout if isinstance(self.timeout, (int, float)) and self.timeout > 0 else DEFAULT_TIMEOUT
        self.reset = [py, str(SEED), *flags, "--reset", "--run-id", rid]
        self.seed = [py, str(SEED), *flags, "--scenario", sid, "--run-id", rid]
        self.prepare = [py, str(SCAFFOLD), "prepare", "--manifest", str(self.manifest),
                        "--stage-dir", str(self.stage_dir), "--backend", args.backend]
        self.stage = [py, str(SCAFFOLD), "stage", "--case", f"{args.skill}-{sid}",
                      "--workspace", str(self.workspace), "--home", str(home)]
        self.launch_mode = args.launch
        self.launch = self.launch_argv(case, args.launch)
        self.clean = [py, str(SCAFFOLD), "clean", "--stage-dir", str(self.stage_dir)]
        grade = [py, str(GRADE), "--manifest", str(self.manifest), "--scenario", sid,
                 "--backend", args.backend, "--json", str(self.grade_json)]
        if args.backend == "ledger":
            grade += ["--ledger", str(self.ledger)]
        self.grade = grade
        self.teardown = [py, str(SEED), *flags, "--teardown", rid]
        self.env_extra = {"EVAL_NOT_ME_RUN_ID": rid, "EVAL_NOT_ME_SCENARIO": sid, "EVAL_NOT_ME_BACKEND": args.backend}
        self.prepare_env = {"NOT_ME_FIXTURE_OWNER": args.owner}

    def launch_argv(self, case: "graders.Case", mode: str) -> List[str]:
        """The token-free claude command. print: headless, JSON result. interactive: the operator's terminal."""
        plugin = str(PLUGIN_DIR)
        if mode == "interactive":
            return ["claude", "--plugin-dir", plugin, case.prompt]
        tools = case.meta.get("allowed_tools") or []
        tools = [tools] if isinstance(tools, str) else [str(t) for t in tools]
        argv = ["claude", "-p", case.prompt, "--plugin-dir", plugin, "--output-format", "json"]
        if tools:
            argv += ["--allowedTools", ",".join(tools)]
        return argv

    def session_env(self, base: Dict[str, str], token: str) -> Dict[str, str]:
        """The child env: the fixture token as GH_TOKEN only, an empty gh config dir so the operator's own gh
        login is invisible, and none of the other credentials. Claude uses the interactive login."""
        env = {k: v for k, v in base.items() if k not in STRIPPED_ENV}
        self.gh_config.mkdir(parents=True, exist_ok=True)
        env.update(self.env_extra)
        env.update({"GH_TOKEN": token, "GH_CONFIG_DIR": str(self.gh_config), "GH_PROMPT_DISABLED": "1",
                    "GIT_TERMINAL_PROMPT": "0",
                    # git asks gh for credentials, and gh answers from GH_TOKEN, not from a keychain login
                    "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
                    "GIT_CONFIG_KEY_1": "credential.helper", "GIT_CONFIG_VALUE_1": "!gh auth git-credential"})
        return env

    def commands(self) -> List[List[str]]:
        return [self.reset, self.seed, self.prepare, self.stage, self.launch, self.grade, self.clean, self.teardown]


def find_session_transcript(plan: Plan, session_id: Optional[str], since: float, home: Path) -> Optional[Path]:
    ws = plan.workspace.resolve()
    if session_id:
        found = transcript_mod.find_transcripts(ws, since=since, home=home, session_id=session_id)
        if found:
            return found[0]
    found = transcript_mod.find_transcripts(ws, since=since, home=home)
    return found[0] if found else None


def merge_result(args: argparse.Namespace, plan: Plan, session: Dict[str, Any], report: Optional["graders.Report"],
                 cost: Optional[float], errors: List[str]) -> dict:
    grade = None
    if plan.grade_json.is_file():
        try:
            grade = json.loads(plan.grade_json.read_text(encoding="utf-8"))
        except ValueError:
            errors.append("grade output is not valid JSON")
    if grade is None:
        errors.append("no grade output")
    if report is None:
        errors.append("no transcript grade")
    ok = bool(grade and grade.get("ok")) and bool(report and report.passed) and not errors
    return {"scenario": args.scenario, "skill": args.skill, "run": plan.n, "run_id": plan.rid,
            "backend": args.backend, "repo": args.repo,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session": session, "graders": report.to_dict() if report else None, "cost_usd": cost,
            "grade": grade, "errors": errors, "ok": ok}


def run_once(args: argparse.Namespace, n: int, runner: Runner, rid_factory: Callable[[str, int], str],
             out: Path, say: Callable[[str], None], home: Path, case: "graders.Case") -> int:
    rid = rid_factory(args.scenario, n)
    plan = Plan(args, rid, n, out, case, home)
    # Only `scaffold.py prepare` sees NOT_ME_FIXTURE_TOKEN. The seeder and grader use the operator's own gh login.
    prep_env = dict(os.environ, **plan.prepare_env)
    base = {k: v for k, v in os.environ.items() if k != "NOT_ME_FIXTURE_TOKEN"}
    env = {k: v for k, v in base.items() if k not in STRIPPED_ENV}  # the stage step needs no credentials
    say(f"# run {n} scenario={args.scenario} run-id={rid} launch={args.launch}")
    if args.dry_run:
        for cmd in plan.commands():
            if cmd is plan.stage:
                say(f"+ (would move {global_dir(home)} to {plan.backup_dir} and restore it after the run)")
            say("+ " + quote(cmd))
            if cmd is plan.launch:
                say(f"+ (in {plan.workspace}; GH_TOKEN from the staged token, GH_CONFIG_DIR={plan.gh_config})")
        say(f"+ (would write {plan.session_json}, {plan.graders_json} and merge into {plan.result})")
        return 0
    out.mkdir(parents=True, exist_ok=True)
    errors: List[str] = []
    session: Dict[str, Any] = {"mode": args.launch, "command": quote(plan.launch)}
    report: Optional[graders.Report] = None
    cost: Optional[float] = None
    try:
        for label, cmd in (("reset", plan.reset), ("seed", plan.seed)):
            say("+ " + quote(cmd))
            if runner(cmd, base) != 0:
                raise RuntimeError(f"{label} failed")
        say("+ " + quote(plan.prepare))
        if runner(plan.prepare, prep_env) != 0:
            raise RuntimeError("prepare failed (is NOT_ME_FIXTURE_TOKEN a dedicated fixture token?)")
        say(f"+ (moving {global_dir(home)} to {plan.backup_dir})")
        backup_global(home, plan.backup_record)
        say("+ " + quote(plan.stage))
        if runner(plan.stage, env) != 0:
            raise RuntimeError("stage failed")
        try:
            token = plan.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"cannot read the staged token: {exc}")
        if not token:
            raise RuntimeError("the staged token file is empty")
        child_env = plan.session_env(base, token)
        since = time.time() - 2
        say("+ " + quote(plan.launch))
        if args.launch == "print":
            code = runner(plan.launch, child_env, cwd=plan.workspace, stdout_path=plan.session_json,
                          stdin_devnull=True, timeout=plan.timeout)
            info = read_session_json(plan.session_json)
            if code == 124:
                errors.append(f"claude timed out after {plan.timeout:g} s")
            elif code != 0:
                errors.append(f"claude exited {code}")
            if info.get("is_error"):
                errors.append("claude reported is_error in its result")
            cost = info.get("total_cost_usd") if isinstance(info.get("total_cost_usd"), (int, float)) else None
            session.update({"session_id": info.get("session_id"), "turns": info.get("num_turns"),
                            "is_error": info.get("is_error")})
        else:
            code = runner(plan.launch, child_env, cwd=plan.workspace)
            if code != 0:
                errors.append(f"claude exited {code}")
            session.update({"session_id": None, "turns": None, "is_error": None})
        path = find_session_transcript(plan, session.get("session_id"), since, home)
        session["transcript"] = str(path) if path else None
        if path is None:
            errors.append(f"no transcript found under {transcript_mod.project_dir_for(plan.workspace.resolve(), home)}")
        else:
            report = graders.grade(case, transcript_mod.load(path), plan.workspace)
            plan.graders_json.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
            say(graders.format_table(report))
        say("+ " + quote(plan.grade))
        grade_exit = runner(plan.grade, base)
        if grade_exit not in (0, 1):
            errors.append(f"grade_state exited {grade_exit}")
    except (RuntimeError, OSError) as exc:
        errors.append(str(exc))
    finally:
        say("+ " + quote(plan.clean))
        try:
            if runner(plan.clean, base) != 0:
                errors.append("scaffold clean failed: remove the stage directory by hand, it holds a token")
        except OSError as exc:
            errors.append(f"scaffold clean failed: {exc}")
        if not args.keep_temp:
            try:
                _force_remove(plan.workspace)
            except OSError as exc:
                errors.append(f"could not delete the workspace {plan.workspace}: {exc}")
        if plan.backup_record.is_file():
            try:
                say("+ " + restore_global(plan.backup_record))
            except (OSError, ValueError, KeyError) as exc:
                errors.append(f"could not restore {global_dir(home)}: {exc}. Run "
                              f"`run_live.py --restore-global {rid}` before anything else")
        say("+ " + quote(plan.teardown))
        try:
            if runner(plan.teardown, base) != 0:
                errors.append("teardown failed: the fixture may need --reset")
        except OSError as exc:
            errors.append(f"teardown failed: {exc}")
    result = merge_result(args, plan, session, report, cost, errors)
    plan.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    say(f"result: {plan.result} ok={result['ok']}")
    return 0 if result["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--skill", help="skill name; the case is <skill>-<scenario>")
    ap.add_argument("--scenario")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--backend", choices=("github", "ledger"), default="github")
    ap.add_argument("--launch", choices=("print", "interactive"), default="print",
                    help="print: headless `claude -p`, no terminal needed (default). interactive: `claude` in "
                         "your terminal; you watch and exit it when the skill is done")
    ap.add_argument("--repo", default=os.environ.get("NOT_ME_FIXTURE_REPO"))
    ap.add_argument("--companion", default=os.environ.get("NOT_ME_FIXTURE_COMPANION"))
    ap.add_argument("--owner", default=os.environ.get("NOT_ME_FIXTURE_OWNER"))
    ap.add_argument("--out", default=str(ROOT / "results" / "evals" / time.strftime("%Y-%m-%d")),
                    help="result directory (default: results/evals/<today>)")
    ap.add_argument("--keep-temp", action="store_true", help="do not delete the run's workspace afterwards")
    ap.add_argument("--ledger", help="ledger JSONL for --backend ledger (default: the workspace's "
                                     ".git/not-me/logs/intended-writes.jsonl)")
    ap.add_argument("--restore-global", metavar="RUN_ID",
                    help="put back the ~/.claude/not-me that a run which died had moved aside, then exit")
    ap.add_argument("--dry-run", action="store_true", help="print the commands and run nothing")
    return ap


def main(argv: Optional[Sequence[str]] = None, runner: Runner = subprocess_runner,
         rid_factory: Callable[[str, int], str] = new_run_id, out: Any = None, home: Optional[Path] = None) -> int:
    args = build_parser().parse_args(argv)
    stream = out or sys.stdout
    home = Path(home) if home is not None else Path.home()

    def say(line: str) -> None:
        print(redact(line), file=stream)

    if args.restore_global:
        record = STATE_DIR / args.restore_global / BACKUP_RECORD
        if not record.is_file():
            print(f"error: no backup record at {record}", file=sys.stderr)
            return 1
        try:
            say(restore_global(record))
        except (OSError, ValueError, KeyError) as exc:
            print(f"error: could not restore: {exc}", file=sys.stderr)
            return 1
        return 0
    missing = [f"--{k}" for k in ("skill", "scenario", "repo", "owner") if not getattr(args, k)]
    if missing:
        print(f"error: {', '.join(missing)} required (--repo and --owner also come from "
              "NOT_ME_FIXTURE_REPO / NOT_ME_FIXTURE_OWNER)", file=sys.stderr)
        return 2
    case_dir = CASES_DIR / f"{args.skill}-{args.scenario}"
    try:
        case = graders.load_case(case_dir)
    except graders.CaseError as exc:
        print(f"error: no usable case {case_dir.name}: {exc}", file=sys.stderr)
        return 2
    if not args.dry_run:
        stale = sorted(STATE_DIR.glob(f"*/{BACKUP_RECORD}"))
        if stale:
            rid = stale[0].parent.name
            print(f"error: a previous run ({rid}) died with your ~/.claude/not-me moved aside. Run "
                  f"`python3 evals-harness/run_live.py --restore-global {rid}` before anything else.", file=sys.stderr)
            return 2
        if not os.environ.get("NOT_ME_FIXTURE_TOKEN"):
            print("error: NOT_ME_FIXTURE_TOKEN is not set (a fine-grained token for the fixture repos only; "
                  "see docs/testing.md)", file=sys.stderr)
            return 2
    if args.runs < 1:
        print("error: --runs must be at least 1", file=sys.stderr)
        return 2
    outdir = Path(args.out)
    taken = [int(p.stem.rsplit("-", 1)[1]) for p in outdir.glob(f"{args.scenario}-*.json")
             if p.stem.rsplit("-", 1)[1].isdigit()] if outdir.is_dir() else []
    first = max(taken, default=0) + 1  # a second invocation the same day adds runs instead of overwriting
    worst = 0
    for n in range(first, first + args.runs):
        worst = max(worst, run_once(args, n, runner, rid_factory, outdir, say, home, case))
    return worst


if __name__ == "__main__":
    sys.exit(main())
