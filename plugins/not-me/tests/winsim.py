"""A Windows file-system view of the POSIX fixture tree, for running guard tests through ntpath on macOS and Linux.

The fixture root (and the plugin root) are shown to the guard under Windows-shaped names such as
C:\\Users\\runneradmin\\AppData\\Local\\Temp\\notme-guard-x, with ntpath as its path module: drive letters,
case-insensitive normcase, "\\" and "/" both separators. Every file-system call the guard makes (exists, realpath,
stat, listdir, open, glob, pathlib reads, git) is translated to the real POSIX path and its result back again, so
the decisions are the ones real Windows would make, not the ones a posixpath that also splits on "\\" makes.
"""
from __future__ import annotations

import builtins
import glob as real_glob
import ntpath
import os
import subprocess
import types
from pathlib import Path, PureWindowsPath
from typing import Any, Iterator, List, Mapping, Optional, Tuple
from unittest import mock

from notme import guard, merge_grant, runlog

WIN_TEMP = "C:\\Users\\runneradmin\\AppData\\Local\\Temp"
WIN_PLUGIN = "D:\\a\\not-me\\not-me\\plugins\\not-me"


class WinSim:
    """Maps real POSIX prefixes to Windows names; start() swaps the guard's path, os, open, glob and Path."""

    def __init__(self, maps: List[Tuple[str, str]], env: Mapping[str, str]):
        self.maps = [(w, str(r)) for w, r in maps]  # (windows prefix, real posix prefix)
        self.real_env = dict(env)
        self.env = {k: self.win_text(v) for k, v in env.items()}
        self._patches: List[Any] = []

    # --- translation -------------------------------------------------------

    def to_real(self, p: Any) -> str:
        q = os.fspath(p).replace("/", "\\")
        low = q.lower()
        for w, r in self.maps:
            if low == w.lower() or low.startswith(w.lower() + "\\"):
                return r + q[len(w):].replace("\\", "/")
        if ntpath.splitdrive(q)[0]:
            return "/nonexistent-windows-drive/" + q.replace(":", "").replace("\\", "/")
        return os.fspath(p)

    def to_win(self, p: Any) -> str:
        s = os.fspath(p)
        for w, r in self.maps:
            if s == r or s.startswith(r + "/"):
                return w + s[len(r):].replace("/", "\\")
        return s

    def win_text(self, text: str) -> str:
        """Text with every real prefix spelt the Windows way (backslashes), as Windows programs print paths."""
        for w, r in self.maps:
            text = text.replace(r, w)
        return text

    # --- the swapped modules -----------------------------------------------

    def path_module(self) -> types.ModuleType:
        sim = self
        mod = types.ModuleType("winsim_ntpath")
        for name in dir(ntpath):
            if not name.startswith("__"):
                setattr(mod, name, getattr(ntpath, name))

        def _fs(fn):
            return lambda p: fn(sim.to_real(p))
        for name in ("exists", "lexists", "isdir", "isfile", "islink", "getsize", "getmtime"):
            setattr(mod, name, _fs(getattr(os.path, name)))

        def abspath(p):
            p = os.fspath(p)
            return ntpath.normpath(p if ntpath.isabs(p) else ntpath.join(sim.to_win(os.getcwd()), p))

        def realpath(p):
            # Windows: absolute and lexically normalised first, then links resolved for the part that exists
            a = abspath(p)
            real = sim.to_real(a)
            if real.startswith("/nonexistent-windows-drive/"):
                return a
            return sim.to_win(os.path.realpath(real))

        def samefile(a, b):
            return os.path.samefile(sim.to_real(a), sim.to_real(b))
        mod.abspath, mod.realpath, mod.samefile = abspath, realpath, samefile
        return mod

    def path_class(self) -> type:
        sim = self

        class WinPath(PureWindowsPath):
            def _real(self) -> Path:
                return Path(sim.to_real(str(self)))

            def is_dir(self) -> bool:
                return self._real().is_dir()

            def is_file(self) -> bool:
                return self._real().is_file()

            def exists(self) -> bool:
                return self._real().exists()

            def read_text(self, encoding: Optional[str] = None, errors: Optional[str] = None) -> str:
                text = self._real().read_text(encoding=encoding, errors=errors)
                for w, r in sim.maps:  # git on Windows writes C:/Users/... into .git files
                    text = text.replace(r, w.replace("\\", "/"))
                return text

            def iterdir(self) -> Iterator["WinPath"]:
                for name in os.listdir(self._real()):
                    yield self / name
        return WinPath

    def os_module(self, path_mod: types.ModuleType) -> types.SimpleNamespace:
        sim = self
        ns = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
        ns.path, ns.sep, ns.altsep = path_mod, "\\", "/"
        ns.stat = lambda p, *a, **k: os.stat(sim.to_real(p), *a, **k)
        ns.lstat = lambda p, *a, **k: os.lstat(sim.to_real(p), *a, **k)
        ns.listdir = lambda p=".": os.listdir(sim.to_real(p))
        ns.getcwd = lambda: sim.to_win(os.getcwd())
        return ns

    def git(self, args: List[str], cwd: Any) -> Optional[str]:
        args = [self.to_real(a) if ntpath.splitdrive(str(a))[0] else a for a in args]
        try:
            proc = subprocess.run(["git", *args], cwd=self.to_real(cwd), capture_output=True, text=True, timeout=10,
                                  stdin=subprocess.DEVNULL, env=self.real_env)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        out = proc.stdout.strip() if proc.returncode == 0 else ""
        return self.win_text(out) or None

    def start(self) -> None:
        sim = self
        path_mod = self.path_module()
        real_read, real_consume, real_append = merge_grant.read, merge_grant.consume, runlog.guard_log_append
        globber = types.SimpleNamespace(
            iglob=lambda p, **k: (sim.to_win(m) for m in real_glob.iglob(sim.to_real(p), **k)))
        self._patches = [
            mock.patch.object(guard, "_path", path_mod),
            mock.patch.object(guard, "os", self.os_module(path_mod)),
            mock.patch.object(guard, "Path", self.path_class()),
            mock.patch.object(guard, "glob", globber),
            mock.patch.object(guard, "open", lambda p, *a, **k: builtins.open(sim.to_real(p), *a, **k), create=True),
            mock.patch.object(merge_grant, "read", lambda home, sid, *a, **k: real_read(sim.to_real(home), sid, *a, **k)),
            mock.patch.object(merge_grant, "consume", lambda home, sid: real_consume(sim.to_real(home), sid)),
            mock.patch.object(runlog, "guard_log_append", lambda p, f: real_append(sim.to_real(p), f)),
            mock.patch.dict(os.environ, self.env, clear=True),  # ntpath.expanduser reads USERPROFILE here
        ]
        for p in self._patches:
            p.start()

    def stop(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._patches = []

    def context(self, ctx: "guard.GuardContext") -> "guard.GuardContext":
        """The Windows view of a context built on the real tree (call while started)."""
        def w(p):
            return None if p is None else self.to_win(p)
        Win = guard.Path
        win = guard.GuardContext(
            cwd=w(ctx.cwd), root=w(ctx.root), git_dir=ctx.git_dir and Win(w(ctx.git_dir)),
            common_dir=ctx.common_dir and Win(w(ctx.common_dir)), config_dir=Win(w(ctx.config_dir)),
            allowlist_path=Win(w(ctx.allowlist_path)), active_run=ctx.active_run, plugin_root=Win(w(ctx.plugin_root)),
            home=Win(w(ctx.home)), env={k: self.win_text(v) for k, v in ctx.env.items()}, git=self.git,
            write_ctx=ctx.write_ctx)
        if ctx.only_self:
            win.self_only()
        return win


def fixture_sim(root: Path, env: Mapping[str, str]) -> WinSim:
    """The Windows view of a guard_cases fixture tree and the plugin root."""
    from tests import guard_cases as gc
    return WinSim([(f"{WIN_TEMP}\\{Path(root).name}", str(Path(root).resolve())), (WIN_PLUGIN, str(gc.PLUGIN))],
                  env)
