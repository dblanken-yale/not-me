"""Schema upgrades for config docs."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple

CURRENT_SCHEMA = 1

Step = Callable[[str, Dict[str, Any], str], Tuple[Dict[str, Any], str, List[str]]]

# from_schema -> step that returns the doc at from_schema + 1 (or later).
REGISTRY: Dict[int, Step] = {}


class MigrationError(ValueError):
    pass


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def schema_of(meta: Dict[str, Any]) -> Any:
    return meta.get("schema", CURRENT_SCHEMA)


def needs_upgrade(meta: Dict[str, Any]) -> bool:
    schema = schema_of(meta)
    return _is_int(schema) and schema < CURRENT_SCHEMA


def upgrade(doc_name: str, meta: Dict[str, Any], body: str) -> Tuple[Dict[str, Any], str, List[str]]:
    """Bring a doc to CURRENT_SCHEMA. Returns (meta, body, notes for the user)."""
    schema = schema_of(meta)
    if not _is_int(schema):
        raise MigrationError(f"{doc_name}: schema must be an integer, got {schema!r}")
    if schema > CURRENT_SCHEMA:
        raise MigrationError(f"{doc_name}: schema {schema} is newer than this plugin supports ({CURRENT_SCHEMA})")
    notes: List[str] = []
    while schema < CURRENT_SCHEMA:
        step = REGISTRY.get(schema)
        if step is None:
            raise MigrationError(f"{doc_name}: no upgrade from schema {schema}")
        meta, body, step_notes = step(doc_name, dict(meta), body)
        notes.extend(step_notes)
        new_schema = schema_of(meta)
        if not _is_int(new_schema) or new_schema <= schema:
            raise MigrationError(f"{doc_name}: upgrade from schema {schema} did not advance the schema")
        schema = new_schema
    return meta, body, notes
