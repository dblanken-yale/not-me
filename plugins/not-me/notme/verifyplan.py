"""What a run verifies and when: targeted tests during the work, the full set once, the baseline, the CI wait.

Pure decisions over facts the CLI gathers (`notme verify-plan`). No default value lives here: the setting and the
CI wait come from the config layers.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .feedback import checks_state
from .frontmatter import iter_unfenced_lines
from .layers import split_sections

TARGETED_HEADING = "Targeted tests"  # the verify.md H2 that pins the targeted test commands


def targeted_commands(verify_body: str) -> List[str]:
    """The command lines of the fenced code blocks in the `## Targeted tests` section, without blank lines and
    `#` comments. Empty when the section is missing, holds no fenced block, or only comments."""
    for heading, text in split_sections(verify_body or "")[1]:
        if heading.casefold() != TARGETED_HEADING.casefold():
            continue
        commands: List[str] = []
        for line, fenced in iter_unfenced_lines(text):
            stripped = line.strip()
            if fenced and stripped and not stripped.startswith(("```", "~~~", "#")):
                commands.append(stripped)
        return commands
    return []


def ci_state(check_runs: Sequence[Dict[str, Any]], statuses: Sequence[Dict[str, Any]], *,
             runs_total: Any = None, statuses_total: Any = None) -> str:
    """success, failure, pending or none for a commit's check runs and commit statuses together; unknown when
    GitHub's total_count says either list holds more than the one page read."""
    for total, items in ((runs_total, check_runs), (statuses_total, statuses)):
        if isinstance(total, int) and not isinstance(total, bool) and total > len(items):
            return "unknown"
    return checks_state(list(check_runs) + list(statuses))


def _baseline(setting: Any, base: str, base_sha: Optional[str], checked_sha: Optional[str],
              ci: str) -> Dict[str, Any]:
    if setting != "auto":
        return {"setting": setting, "run": True, "reason": f"verify.baseline is {setting}: the baseline always runs"}
    if not checked_sha:
        where = f"{base} (no local origin/{base})"
    elif checked_sha == base_sha:
        where = f"{base} at {checked_sha}"
    else:
        where = f"commit {checked_sha}"
    if ci == "success" and checked_sha:
        return {"setting": setting, "run": False,
                "reason": f"verify.baseline is auto and CI on {where} is green: baseline skipped"}
    return {"setting": setting, "run": True,
            "reason": f"verify.baseline is auto but CI on {where} is {ci}: the baseline runs"}


def plan(*, setting: Any, base: str, base_sha: Optional[str], checked_sha: Optional[str], base_ci: str,
         verify_body: str, ci_wait_minutes: Any) -> Dict[str, Any]:
    """The verify plan for a run. checked_sha is the commit whose CI base_ci reports: the base head, or the PR
    head on a rework run. The baseline is skipped only with `auto` and green CI for a known checked_sha;
    targeted tests are configured only when the section pins at least one command; a CI wait of 0 (or less)
    means do not wait."""
    commands = targeted_commands(verify_body)
    no_wait = isinstance(ci_wait_minutes, (int, float)) and not isinstance(ci_wait_minutes, bool) \
        and ci_wait_minutes <= 0
    return {
        "base": base, "base_sha": base_sha, "checked_sha": checked_sha, "base_ci": base_ci,
        "baseline": _baseline(setting, base, base_sha, checked_sha, base_ci),
        "targeted": {"configured": bool(commands), "commands": commands},
        "ci_wait_minutes": ci_wait_minutes, "ci_wait": not no_wait,
    }
