"""Load, explain and check the layered not-me configuration."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from . import migrations, paths, roster
from .frontmatter import FrontmatterError, parse_with_lines
from .layers import (DOC_NAMES, LAYER_ORDER, SCHEMA, Doc, KeyPath, Layers, Schema, confirmed, discover, dotted,
                     merge_meta, resolve_body, spec_for)

NO_LAYERS_HINT = "no global or repo not-me config found; using the shipped defaults (run setup to create one)"


@dataclass
class Config:
    meta: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    bodies: Dict[str, str] = field(default_factory=dict)
    sources: Dict[str, dict] = field(default_factory=dict)
    body_sources: Dict[str, Dict[str, str]] = field(default_factory=dict)
    body_layers: Dict[str, List[str]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    layers: Layers = field(default_factory=Layers)
    docs: Dict[str, Dict[str, Doc]] = field(default_factory=dict)
    project_dir: Optional[Path] = None
    home: Optional[Path] = None
    plugin_root: Optional[Path] = None
    _errors: List[str] = field(default_factory=list)

    def display(self, path: Optional[Path]) -> Optional[str]:
        if path is None:
            return None
        return paths.display_path(path, self.project_dir, self.home, self.plugin_root)

    def explain(self) -> Dict[str, Any]:
        """Every effective key with where it came from, for people and JSON."""
        keys: Dict[str, Any] = {}
        for name in sorted(self.sources):
            src = self.sources[name]
            entry: Dict[str, Any] = {
                "value": _get(self.meta, name),
                "source": {"layer": src["layer"], "path": src["path"], "line": src["line"]},
                "shadowed": src["shadowed"],
            }
            for extra in ("contributors", "removed", "ignored"):
                if extra in src:
                    entry[extra] = src[extra]
            keys[name] = copy.deepcopy(entry)
        bodies = {
            name: {"layers_used": list(self.body_layers.get(name, [])),
                   "sections": dict(self.body_sources.get(name, {}))}
            for name in DOC_NAMES
        }
        return {
            "layers": {layer: self.display(self.layers.dirs[layer]) for layer in LAYER_ORDER},
            "keys": keys,
            "bodies": bodies,
            "warnings": list(self.warnings),
        }

    def check(self) -> List[str]:
        """Errors as "<layer> <path>:<line>: <message>"; empty when valid."""
        errors = list(self._errors)
        for name in DOC_NAMES:
            docs = self.docs.get(name, {})
            reference = docs["default"].meta if "default" in docs else {}
            for layer in LAYER_ORDER:
                if layer in docs:
                    _check_map(docs[layer], docs[layer].meta, reference, (), SCHEMA.get(name, {}), errors)
        return errors

    def effective_roster(self, operator: Optional[str] = None) -> roster.Roster:
        """The roster in force; operator defaults to project.operator unless it is auto."""
        if operator is None:
            configured = self.meta.get("project", {}).get("operator")
            if isinstance(configured, str) and configured != "auto":
                operator = configured
        return roster.effective(roster.parse_roster(self.bodies.get("project", "")), operator)


def _get(meta: Mapping[str, Any], name: str) -> Any:
    """Value at a dotted name; tries the longest key first so keys may contain dots."""
    if name in meta:
        return meta[name]
    parts = name.split(".")
    for i in range(len(parts) - 1, 0, -1):
        head = dotted(tuple(parts[:i]))
        if isinstance(meta.get(head), dict):
            return _get(meta[head], dotted(tuple(parts[i:])))
    return None


# --- check -----------------------------------------------------------------


def _fmt(doc: Doc, key_path: KeyPath, message: str) -> str:
    line = doc.lines.get(key_path)
    while line is None and len(key_path) > 1:
        key_path = key_path[:-1]
        line = doc.lines.get(key_path)
    return f"{doc.layer} {doc.path}:{line or 1}: {message}"


def _type_name(value: Any) -> str:
    for kind, label in ((bool, "true/false"), (int, "integer"), (float, "number"), (str, "string"),
                        (list, "list"), (dict, "map")):
        if isinstance(value, kind):
            return label
    return "null"


def _same_type(value: Any, ref: Any) -> bool:
    got, want = _type_name(value), _type_name(ref)
    return got == want or (want == "number" and got == "integer")


def _check_map(doc: Doc, meta: Mapping[str, Any], ref: Mapping[str, Any], prefix: KeyPath, schema: Schema,
               out: List[str]) -> None:
    for key, value in meta.items():
        key_path = prefix + (key,)
        name = dotted(key_path)
        spec = spec_for(schema, key_path)
        expected = ref.get(key)
        if value is None:
            continue
        if spec.values is not None:
            if not isinstance(value, dict):
                out.append(_fmt(doc, key_path, f"{name} must be a map"))
                continue
            for k, v in value.items():
                if not isinstance(v, spec.values):
                    out.append(_fmt(doc, key_path + (k,), f"{name}.{k} must be a {_type_name(spec.values())}"))
            continue
        if expected is not None and not _same_type(value, expected):
            out.append(_fmt(doc, key_path, f"{name} must be a {_type_name(expected)}, got {_type_name(value)}"))
            continue
        if spec.enum and value not in spec.enum:
            out.append(_fmt(doc, key_path, f"{name} must be one of {', '.join(spec.enum)}, got {value!r}"))
        elif not confirmed(spec, value, key, doc.meta):
            out.append(_fmt(doc, key_path, f"{name} is {value} but '{key}' is not listed in "
                                           f"{spec.confirm.list_key} in this file, so it is ignored"))
        if spec.item_key and isinstance(value, list):
            for i, item in enumerate(value):
                if not isinstance(item, dict) or not isinstance(item.get(spec.item_key), str) or not item[spec.item_key]:
                    out.append(_fmt(doc, key_path, f"{name}[{i}] must be a map with a {spec.item_key}"))
        if isinstance(value, dict) and isinstance(expected, dict):
            _check_map(doc, value, expected, key_path, schema, out)


# --- loading ---------------------------------------------------------------


def _read_doc(name: str, layer: str, directory: Path, cfg: Config) -> Optional[Doc]:
    fpath = directory / f"{name}.md"
    shown = cfg.display(fpath)
    try:
        text = fpath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        cfg.warnings.append(f"{layer} {shown}: unreadable ({exc.__class__.__name__}); ignoring it")
        return None
    try:
        meta, body, lines = parse_with_lines(text, source=shown)
    except FrontmatterError as exc:
        msg = f"{layer} {shown}:{exc.line or 1}: {exc.message}"
        cfg._errors.append(msg)
        cfg.warnings.append(f"{msg}; ignoring this file and using lower layers")
        return None
    if migrations.schema_of(meta) != migrations.CURRENT_SCHEMA:
        try:
            meta, body, notes = migrations.upgrade(name, meta, body)
        except migrations.MigrationError as exc:
            msg = f"{layer} {shown}:{lines.get(('schema',), 1)}: unknown schema: {exc}"
            cfg._errors.append(msg)
            cfg.warnings.append(f"{msg}; ignoring this file and using lower layers")
            return None
        cfg.warnings.extend(f"{layer} {shown}: {note}" for note in notes)
    extends = meta.pop("extends", None)
    return Doc(layer, shown, meta, body, lines, extends=extends)


def load(project_dir: Optional[os.PathLike] = None, home: Optional[os.PathLike] = None,
         plugin_root: Optional[os.PathLike] = None, env: Optional[Mapping[str, str]] = None) -> Config:
    """Read all three layers and merge them. Never raises on bad config files."""
    root = Path(plugin_root) if plugin_root else paths.plugin_root(env)
    home_p = Path(home) if home else paths.home_dir(env)
    proj = paths.project_dir(project_dir, env)
    if proj is not None:
        proj = paths.main_checkout(proj.resolve()) if proj.is_dir() else None

    cfg = Config(project_dir=proj, home=home_p, plugin_root=root)
    cfg.layers = discover(proj, home_p, plugin_root=root)
    cfg.warnings.extend(cfg.layers.warnings)
    if cfg.layers.dirs["global"] is None and cfg.layers.dirs["repo"] is None and not cfg.layers.warnings:
        cfg.warnings.append(NO_LAYERS_HINT)

    for name in DOC_NAMES:
        docs: Dict[str, Doc] = {}
        for layer in LAYER_ORDER:
            directory = cfg.layers.dirs[layer]
            if directory is not None and name in cfg.layers.files[layer]:
                doc = _read_doc(name, layer, directory, cfg)
                if doc is not None:
                    docs[layer] = doc
        cfg.docs[name] = docs
        merged, sources = merge_meta(
            *(docs[layer].meta if layer in docs else None for layer in LAYER_ORDER),
            schema=SCHEMA.get(name, {}),
            paths={layer: d.path for layer, d in docs.items()},
            lines={layer: d.lines for layer, d in docs.items()},
            warnings=cfg.warnings,
        )
        cfg.meta[name] = merged
        cfg.sources.update({f"{name}.{key}": src for key, src in sources.items()})
        cfg.bodies[name], cfg.body_sources[name], cfg.body_layers[name] = resolve_body(name, docs, cfg.warnings)
    cfg.warnings.extend(roster.bot_row_warnings(roster.parse_roster(cfg.bodies.get("project", ""))))
    return cfg
