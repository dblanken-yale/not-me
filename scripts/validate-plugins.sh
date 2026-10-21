#!/usr/bin/env bash
# Validate the layout of every plugin under plugins/.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0

err() { echo "ERROR: $*" >&2; fail=1; }

for dir in "$root"/plugins/*/; do
  [ -d "$dir" ] || continue
  name="$(basename "$dir")"
  manifest="$dir.claude-plugin/plugin.json"

  if [ ! -f "$manifest" ]; then
    err "$name: missing .claude-plugin/plugin.json"
  else
    if ! python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
missing = [k for k in ("name", "description", "author") if not d.get(k)]
if missing:
    print("missing fields: " + ", ".join(missing))
    sys.exit(1)
' "$manifest"; then
      err "$name: plugin.json invalid"
    fi
  fi

  [ -f "$dir/README.md" ] || err "$name: missing README.md"

  shopt -s nullglob
  skills=("$dir"skills/*/SKILL.md)
  shopt -u nullglob
  if [ "${#skills[@]}" -eq 0 ]; then
    echo "WARNING: $name: no skills yet" >&2
  fi
  for skill in "${skills[@]}"; do
    front="$(awk 'NR==1 && $0!="---"{exit} NR>1 && $0=="---"{exit} NR>1{print}' "$skill")"
    echo "$front" | grep -q '^name:' || err "$skill: missing name: in frontmatter"
    echo "$front" | grep -q '^description:' || err "$skill: missing description: in frontmatter"
  done
done

if [ "$fail" -ne 0 ]; then
  exit 1
fi
echo "plugins OK"
