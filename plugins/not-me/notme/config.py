"""Load, explain and check the layered not-me configuration."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from . import humans, migrations, paths, roster
from .frontmatter import FrontmatterError, parse_with_lines
from .layers import (DOC_NAMES, LAYER_ORDER, SCHEMA, Doc, KeyPath, Layers, Schema, confirmed, discover, dotted,
                     item_ok, merge_meta, resolve_body, spec_for, split_sections)

NO_LAYERS_HINT = "no global or repo not-me config found; using the shipped defaults (run setup to create one)"
PICKING_HEADING = "Picking work"  # the project.md H2 that holds the picking rules in plain words
OLD_PRIORITY_FIELDS = ("queue", "board", "priority_fields")  # replaced by picking.fields


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
    # the warning when the layers say `mode: live` but the token-signed marker does not confirm it (then the
    # effective mode is dry-run); None otherwise
    live_unconfirmed: Optional[str] = None
    # warnings about how issues are picked (a deprecated key that was moved), also in warnings
    picking_warnings: List[str] = field(default_factory=list)
    # the board fields the deprecated queue.board.priority_fields named, in its order: a board queue still ranks
    # by them (higher first) so an old config keeps its order; plain picking.fields never rank
    ranked_fields: List[str] = field(default_factory=list)
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
            for extra in ("contributors", "removed", "ignored", "downgraded", "migrated"):
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
            "picking": self.picking(),
            "warnings": list(self.warnings),
        }

    def picking(self) -> Dict[str, Any]:
        """The picking rules in effect: the text of the `## Picking work` section of the effective project.md body
        and the layer it came from (the default layer's text when no higher body has the section), plus
        picking.fields and picking.target_epics. The rules only order eligible issues; they grant nothing."""
        rules, layer = _picking_section(self.bodies.get("project", ""))
        if rules:
            layer = self.body_sources.get("project", {}).get(layer or "")
        else:
            default = self.docs.get("project", {}).get("default")
            rules, _ = _picking_section(default.body if default else "")
            layer = "default" if rules else None
        meta = self.meta.get("project", {}).get("picking")
        meta = meta if isinstance(meta, dict) else {}  # an invalid value is a check() error
        return {"rules": rules or None, "layer": layer,
                "fields": _as_list(meta.get("fields")), "target_epics": _as_list(meta.get("target_epics"))}

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


def _picking_section(body: str) -> tuple:
    """(section text without its heading, heading as written) of the Picking work H2; ("", None) when absent."""
    for heading, text in split_sections(body)[1]:
        if heading.casefold() == PICKING_HEADING.casefold():
            return text.split("\n", 1)[1].strip() if "\n" in text else "", heading
    return "", None


def _as_list(value: Any) -> List[Any]:
    return list(value) if isinstance(value, list) else []


def picking_summary(picking: Mapping[str, Any]) -> str:
    """One line for status: where the rules come from, then the named fields and target epics when set."""
    layer = picking.get("layer")
    parts = ["impact default" if layer in ("default", None) else f"rules from {layer} layer"]
    if picking.get("fields"):
        parts.append("fields: " + ", ".join(map(str, picking["fields"])))
    if picking.get("target_epics"):
        parts.append("target epics: " + ", ".join(f"#{n}" for n in picking["target_epics"]))
    return "; ".join(parts)


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
        if isinstance(value, list) and (spec.items_enum or spec.required_items):
            bad = [i for i in value if i not in spec.items_enum] if spec.items_enum else []
            if bad:
                out.append(_fmt(doc, key_path, f"{name} has unknown {', '.join(map(repr, bad))}; "
                                               f"use only {', '.join(spec.items_enum)}"))
            missing = [i for i in spec.required_items if i not in value]
            if missing:
                out.append(_fmt(doc, key_path, f"{name} must include {', '.join(missing)}"))
        if spec.items_type and isinstance(value, list) and not all(item_ok(spec, i) for i in value):
            out.append(_fmt(doc, key_path, f"{name} must be a list of {spec.items_type}s, got {value!r}"))
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
    _move_priority_fields(cfg)
    _confirm_live(cfg)
    return cfg


def _move_priority_fields(cfg: Config) -> None:
    """queue.board.priority_fields is deprecated: picking.fields names the fields pick-issue gathers, and the
    Picking work section says how to weigh them. When a layer still sets it, its names are appended to
    picking.fields (so board facts and the board's suggested order keep them) and a warning names the file, the
    line and the replacement. Its names, in their own order, are kept in cfg.ranked_fields so a board queue still
    ranks by them as it did before. The key is dropped from the effective config either way."""
    board = (cfg.meta.get("project", {}).get("queue") or {}).get("board")
    if not isinstance(board, dict) or "priority_fields" not in board:
        return
    old = board.pop("priority_fields")
    name = "project." + dotted(OLD_PRIORITY_FIELDS)
    src = cfg.sources.pop(name, None) or {}
    for key in [k for k in cfg.sources if k.startswith(name + ".")]:
        del cfg.sources[key]
    picking = cfg.meta["project"].setdefault("picking", {})
    if not isinstance(picking, dict):
        return  # an invalid picking value is a check() error; nothing to move into
    fields = _as_list(picking.get("fields"))
    old = old if isinstance(old, list) else []
    cfg.ranked_fields = list(old)
    added = [f for f in old if f not in fields]
    if added:
        picking["fields"] = fields + added
        moved = cfg.sources.setdefault("project.picking.fields", {"layer": None, "path": None, "line": None,
                                                                  "shadowed": []})
        moved["migrated"] = {"from": dotted(OLD_PRIORITY_FIELDS), "added": copy.deepcopy(added)}
    doc = cfg.docs.get("project", {}).get(src.get("layer") or "")
    where = _fmt(doc, OLD_PRIORITY_FIELDS, "") if doc else "config: "
    note = (f"{where}{dotted(OLD_PRIORITY_FIELDS)} is deprecated; "
            + (f"{', '.join(map(str, added))} {'was' if len(added) == 1 else 'were'} added to picking.fields. "
               if added else "its fields are already in picking.fields. ")
            + "Move them to `picking: {fields: [...]}` and say how to weigh them in a `## Picking work` section "
              "of project.md")
    cfg.picking_warnings.append(note)
    cfg.warnings.append(note)


def _confirm_live(cfg: Config) -> None:
    """Live mode needs both `mode: live` in the layers (only the global layer can say it) and the marker that
    `notme mode live` signs with the operator token. Without a valid marker the effective mode is dry-run, the
    source records what was configured and why it was downgraded, and a warning says how to go live."""
    _revoke_live_unless_global_live(cfg)
    project = cfg.meta.get("project", {})
    if project.get("mode") != "live" or humans.live_confirmed(cfg.home):
        return
    project["mode"] = "dry-run"
    src = cfg.sources.setdefault("project.mode", {"layer": None, "path": None, "line": None, "shadowed": []})
    reason = ("not confirmed: no valid live marker for the current operator token (hand-edits and a rotated token "
              "do not count)")
    src["downgraded"] = {"configured": "live", "reason": reason}
    where = f"{src.get('path')}:{src.get('line') or 1}" if src.get("path") else "the config"
    cfg.live_unconfirmed = (f"mode: live is set in {where} but not confirmed, so not-me runs in dry-run; "
                            "run /not-me:mode live")
    cfg.warnings.append(cfg.live_unconfirmed)


def _revoke_live_unless_global_live(cfg: Config) -> None:
    """When the global layer's own `mode` is not `live` (absent, dry-run, invalid, or the file is unreadable),
    delete the live marker and rotate the epoch, so returning to dry-run by a hand edit revokes the signature the
    same way `notme mode dry-run --global` does. Only the global layer counts: a repo-layer dry-run pin leaves the
    operator's live signature alone. Cheap and file-free when there is no marker. This also runs inside the guard
    hook (it loads config for write checks); that is fine because it only touches files in the not-me home and
    starts no process. Best effort: a failure becomes a warning, never an exception."""
    if cfg.home is None:
        return
    global_doc = cfg.docs.get("project", {}).get("global")
    if global_doc is not None and global_doc.meta.get("mode") == "live":
        return
    try:
        if not os.path.lexists(str(humans.live_marker_path(cfg.home))):
            return
        humans.clear_live_marker(cfg.home)
    except Exception as exc:  # never let revocation abort config loading
        cfg.warnings.append(f"global mode is not live but could not revoke the live marker {humans.LIVE_MARKER} "
                            f"in the not-me home ({exc.__class__.__name__}); run `notme mode dry-run --global`")
