"""Tests for the not-me configuration core."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "config"
LAYER_CASES = FIXTURES / "layers"


def sign_live(home) -> Path:
    """A valid live-mode marker for home (what `notme mode live` writes), creating the operator token if missing.
    A global `mode: live` counts only next to one."""
    from notme import humans

    Path(home).mkdir(parents=True, exist_ok=True)
    if humans.read_token(home) is None:
        humans.create_token(home)
    return humans.write_live_marker(home)


class TempCase:
    """Copy a layer fixture into a scratch project dir and home dir."""

    def __init__(self, case: str | None = None):
        self.root = Path(tempfile.mkdtemp(prefix="notme-test-")).resolve()
        self.project = self.root / "project"
        self.home = self.root / "home"
        self.project.mkdir()
        if case is not None:
            src = LAYER_CASES / case
            if (src / "global").is_dir():
                shutil.copytree(src / "global", self.home)
            if (src / "repo").is_dir():
                shutil.copytree(src / "repo", self.project / ".claude" / "not-me")

    @property
    def repo_dir(self) -> Path:
        return self.project / ".claude" / "not-me"

    def write(self, layer: str, name: str, text: str) -> Path:
        base = self.home if layer == "global" else self.repo_dir
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{name}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def sign_live(self) -> Path:
        return sign_live(self.home)

    def load(self):
        from notme import config

        return config.load(project_dir=str(self.project), home=str(self.home), plugin_root=str(PLUGIN_ROOT))

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
