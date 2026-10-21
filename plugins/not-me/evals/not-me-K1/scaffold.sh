#!/usr/bin/env bash
# Per-case scaffold. Every plugins/not-me/evals/<case>/scaffold.sh is a copy of
# evals-harness/scaffold/case-scaffold.sh (a test keeps them identical).
# `claude plugin eval --scaffold` runs it with bash, the run's working directory as
# the current directory and the sandbox HOME. It resolves the script inside the case
# directory only, which is why each case carries its own copy.
set -euo pipefail
case_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$case_dir/../../../.." && pwd)"
exec python3 "$root/evals-harness/scaffold/scaffold.py" stage --case "$(basename "$case_dir")"
