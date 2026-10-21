"""Find the three config layers and merge them.

Layers, lowest first: default (shipped with the plugin), global (per
developer), repo (committed in the project). A missing file is never an error.
"""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import paths, roster
from .frontmatter import iter_unfenced_lines

LAYER_ORDER = ("default", "global", "repo")
DOC_NAMES = ("project", "standards", "verify", "review", "prove")
REPO_SUBDIR = (".claude", "not-me")

_H2 = re.compile(r"^##[ \t]+(.+?)[ \t]*#*[ \t]*$")

KeyPath = Tuple[Any, ...]


def dotted(key_path: KeyPath) -> str:
    return ".".join(str(k) for k in key_path)


# --- key schema ------------------------------------------------------------


@dataclass(frozen=True)
class Confirm:
    """A value that only takes effect when the rule is listed under list_key in the same layer."""

    value: Any
    list_key: str


@dataclass(frozen=True)
class KeySpec:
    """How one key merges and validates. Types come from the default layer's value."""

    merge: str = "replace"  # replace | union
    enum: Tuple[Any, ...] = ()
    values: Optional[type] = None  # every value of this map must be this type
    item_key: Optional[str] = None  # every list item must be a map with this key
    confirm: Optional[Confirm] = None


Schema = Mapping[str, KeySpec]

SCHEMA: Dict[str, Dict[str, KeySpec]] = {
    "project": {
        "protected_branches": KeySpec(merge="union"),
        "allow_repos": KeySpec(merge="union"),
        "markers.legacy": KeySpec(merge="union"),
        "mode": KeySpec(enum=("dry-run", "live")),
        "models.strategy": KeySpec(enum=("orchestrate", "single")),
        "labels": KeySpec(values=str),
        "repos": KeySpec(item_key="name"),
        "guard.*": KeySpec(enum=("on", "off"), confirm=Confirm("off", "confirm_guard_off")),
    },
}

_DEFAULT_SPEC = KeySpec()


def spec_for(schema: Schema, key_path: KeyPath) -> KeySpec:
    """The spec for a key: exact dotted match, else `<parent>.*`."""
    return schema.get(dotted(key_path)) or schema.get(dotted(key_path[:-1] + ("*",))) or _DEFAULT_SPEC


def confirmed(spec: KeySpec, value: Any, rule: Any, layer_meta: Mapping[str, Any]) -> bool:
    """False when value needs a confirmation that layer_meta does not give."""
    if spec.confirm is None or value != spec.confirm.value:
        return True
    listed = layer_meta.get(spec.confirm.list_key)
    return isinstance(listed, list) and rule in listed


# --- discovery -------------------------------------------------------------


@dataclass
class Layers:
    dirs: Dict[str, Optional[Path]] = field(default_factory=lambda: {layer: None for layer in LAYER_ORDER})
    files: Dict[str, List[str]] = field(default_factory=lambda: {layer: [] for layer in LAYER_ORDER})
    warnings: List[str] = field(default_factory=list)


@dataclass
class Doc:
    """One config file in one layer."""

    layer: str
    path: str  # display form, never absolute
    meta: Dict[str, Any]
    body: str
    lines: Dict[KeyPath, int] = field(default_factory=dict)
    extends: Optional[str] = None


def _probe(path: Optional[Path], label: str, display: str, warnings: List[str]) -> Tuple[Optional[Path], List[str]]:
    if path is None:
        return None, []
    try:
        if not path.exists():
            return None, []
        real = path.resolve(strict=True)
        if not real.is_dir():
            warnings.append(f"{label} config path {display} is not a directory; ignoring it")
            return None, []
        entries = set(os.listdir(real))
    except OSError as exc:
        reason = exc.strerror or exc.__class__.__name__
        warnings.append(f"{label} config dir {display} is unreadable ({reason}); treating it as absent")
        return None, []
    return real, [name for name in DOC_NAMES if f"{name}.md" in entries]


def discover(project_dir: Optional[os.PathLike], home: Optional[os.PathLike], *,
             plugin_root: Optional[os.PathLike] = None) -> Layers:
    """Locate the default, global and repo directories and the docs in each."""
    root = Path(plugin_root) if plugin_root else paths.plugin_root()
    proj = Path(project_dir) if project_dir else None
    home_p = Path(home) if home else None
    candidates = {
        "default": root / "defaults",
        "global": home_p,
        "repo": proj.joinpath(*REPO_SUBDIR) if proj else None,
    }
    found = Layers()
    for layer, path in candidates.items():
        shown = paths.display_path(path, proj, home_p, root) if path is not None else ""
        found.dirs[layer], found.files[layer] = _probe(path, layer, shown, found.warnings)
    if found.dirs["default"] is None:
        found.warnings.append("default config dir is missing from the plugin; built-in values are unavailable")
    return found


# --- frontmatter merge -----------------------------------------------------


def _source(layer: Optional[str], path: Optional[str], line: Optional[int], shadowed: List[dict]) -> Dict[str, Any]:
    return {"layer": layer, "path": path, "line": line, "shadowed": shadowed}


def _take_over(target: Dict[str, Any], sources: Dict[str, dict], key: str, name: str) -> List[dict]:
    """Forget the sources of whatever target[key] held; return its shadowed history."""
    prev = sources.pop(name, None)
    for k in [k for k in sources if k.startswith(name + ".")]:
        del sources[k]
    if prev is None:
        return []
    return prev["shadowed"] + [{"layer": prev["layer"], "value": copy.deepcopy(target.get(key))}]


@dataclass
class _Pass:
    """One layer being applied on top of the merge so far."""

    layer: str
    root: Mapping[str, Any]
    schema: Schema
    path: Optional[str]
    lines: Mapping[KeyPath, int]
    warnings: List[str]


def _apply(target: Dict[str, Any], sources: Dict[str, dict], meta: Mapping[str, Any], prefix: KeyPath,
           p: _Pass) -> None:
    for key, value in meta.items():
        key_path = prefix + (key,)
        name = dotted(key_path)
        line = p.lines.get(key_path)
        spec = spec_for(p.schema, key_path)
        if not confirmed(spec, value, key, p.root):
            src = sources.setdefault(name, _source(None, None, None, []))
            src.setdefault("ignored", []).append({"layer": p.layer, "value": value, "reason": "not confirmed"})
        elif spec.merge == "union" and value is None:
            p.warnings.append(f"{p.layer} {p.path}:{line or 1}: {name} is null, but null cannot clear a union list; "
                              "use !value to remove an entry")
        elif spec.merge == "union":
            _apply_union(target, sources, key, name, value, p.layer, p.path, line)
        elif isinstance(value, dict):
            if not isinstance(target.get(key), dict):
                shadowed = _take_over(target, sources, key, name)
                target[key] = {}
                if not value:
                    sources[name] = _source(p.layer, p.path, line, shadowed)
            _apply(target[key], sources, value, key_path, p)
        elif value is None and key in target:
            shadowed = _take_over(target, sources, key, name)
            del target[key]
            sources[name] = dict(_source(p.layer, p.path, line, shadowed), removed=True)
        else:
            shadowed = _take_over(target, sources, key, name)
            target[key] = copy.deepcopy(value)
            sources[name] = _source(p.layer, p.path, line, shadowed)


def _apply_union(target: Dict[str, Any], sources: Dict[str, dict], key: str, name: str, value: Any,
                 layer: str, path: Optional[str], line: Optional[int]) -> None:
    items = value if isinstance(value, list) else [value]
    current = target.get(key)
    acc = list(current) if isinstance(current, list) else []
    for item in items:
        if isinstance(item, str) and item.startswith("!"):
            acc = [x for x in acc if x != item[1:]]
        elif item not in acc:
            acc.append(copy.deepcopy(item))
    target[key] = acc
    src = sources.get(name) or dict(_source(layer, path, line, []), contributors=[])
    src.update(layer=layer, path=path, line=line)
    src["contributors"].append({"layer": layer, "path": path, "line": line, "value": copy.deepcopy(items)})
    sources[name] = src


def merge_meta(default: Optional[Mapping[str, Any]], global_: Optional[Mapping[str, Any]],
               repo: Optional[Mapping[str, Any]], *, schema: Optional[Schema] = None,
               paths: Optional[Mapping[str, str]] = None,
               lines: Optional[Mapping[str, Mapping]] = None,
               warnings: Optional[List[str]] = None) -> Tuple[Dict[str, Any], Dict[str, dict]]:
    """Merge one doc's frontmatter across layers.

    Maps merge per key; scalars and lists are replaced by the higher layer;
    union keys collect values from every layer, where "!value" removes a lower
    value (null on a union key is ignored with a warning, since union lists
    only grow); null in a higher layer removes the lower key; a value that needs
    confirmation and lacks it is ignored (recorded under "ignored"). Returns
    (merged, sources) with sources keyed by dotted path.
    """
    schema = schema or {}
    paths = paths or {}
    lines = lines or {}
    warnings = warnings if warnings is not None else []
    merged: Dict[str, Any] = {}
    sources: Dict[str, dict] = {}
    for layer, meta in zip(LAYER_ORDER, (default, global_, repo)):
        if meta:
            _apply(merged, sources, meta, (), _Pass(layer, meta, schema, paths.get(layer), lines.get(layer, {}),
                                                         warnings))
    return merged, sources


# --- body resolution -------------------------------------------------------

SectionMerger = Callable[[str, str], str]

# Same-named H2 sections normally replace in place; these merge instead.
SECTION_MERGERS: Dict[str, SectionMerger] = {"roster": roster.merge_section}


def split_sections(body: str) -> Tuple[str, List[Tuple[str, str]]]:
    """(preamble, [(heading, section text including its heading line)])."""
    preamble: List[str] = []
    sections: List[Tuple[str, List[str]]] = []
    for line, fenced in iter_unfenced_lines(body):
        m = None if fenced else _H2.match(line)
        if m:
            sections.append((m.group(1).strip(), [line]))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return "\n".join(preamble), [(h, "\n".join(text)) for h, text in sections]


def _compose(preamble: str, sections: List[list]) -> str:
    parts = [preamble.strip("\n")] if preamble.strip() else []
    parts += [text.strip("\n") for _, text, _ in sections]
    return "\n\n".join(parts) + "\n" if parts else ""


def _resolve_from(name: str, layer: str, docs: Mapping[str, Doc], warnings: List[str]):
    """Return (preamble, [[heading, text, layer]], chain of layers used)."""
    doc = docs[layer]
    preamble, own = split_sections(doc.body)
    own_sections = [[h, text, layer] for h, text in own]
    ext = doc.extends
    if ext is None:
        return preamble, own_sections, [layer]
    where = f"{layer} {doc.path}"
    if ext not in ("global", "default"):
        warnings.append(f"{where}: unknown 'extends: {ext}' (use global or default); using this body alone")
        return preamble, own_sections, [layer]
    if LAYER_ORDER.index(ext) >= LAYER_ORDER.index(layer):
        warnings.append(f"{where}: 'extends: {ext}' must name a lower layer; using this body alone")
        return preamble, own_sections, [layer]
    if docs.get(ext) is None:
        warnings.append(f"{where}: 'extends: {ext}' but there is no {ext} {name}.md; using this body alone")
        return preamble, own_sections, [layer]
    base_pre, sections, chain = _resolve_from(name, ext, docs, warnings)
    index = {h.casefold(): i for i, (h, _, _) in enumerate(sections)}
    for heading, text, lay in own_sections:
        i = index.get(heading.casefold())
        if i is None:
            index[heading.casefold()] = len(sections)
            sections.append([heading, text, lay])
            continue
        merger = SECTION_MERGERS.get(heading.casefold())
        sections[i] = [heading, merger(sections[i][1], text) if merger else text, lay]
    return (preamble if preamble.strip() else base_pre), sections, chain + [layer]


def resolve_body(name: str, layer_docs: Mapping[str, Optional[Doc]],
                 warnings: Optional[List[str]] = None) -> Tuple[str, Dict[str, str], List[str]]:
    """Body from the highest layer with the file, following `extends`.

    With `extends: global|default`, the doc's H2 sections replace same-named
    lower sections in place (or merge, see SECTION_MERGERS) and new ones are
    appended. Returns (body, {H2 heading: layer}, layers used lowest first).
    """
    warnings = warnings if warnings is not None else []
    docs = {layer: d for layer, d in layer_docs.items() if d is not None}
    present = [layer for layer in LAYER_ORDER if layer in docs]
    if not present:
        return "", {}, []
    preamble, sections, chain = _resolve_from(name, present[-1], docs, warnings)
    return _compose(preamble, sections), {h: lay for h, _, lay in sections}, chain
