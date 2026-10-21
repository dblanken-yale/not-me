#!/usr/bin/env bash
# Tests for bin/notme-python. Run: bash plugins/not-me/tests/test_launcher.sh
# shellcheck disable=SC2015 # ok() never fails
set -u

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
launcher="$here/../bin/notme-python"
work="$(mktemp -d "${TMPDIR:-/tmp}/notme-launcher.XXXXXX")" || exit 1
trap 'rm -rf "$work"' EXIT

pass=0 fail=0
ok() { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "FAIL: $1"; }

printf 'import sys\nprint("hello", sys.argv[1], sys.version_info >= (3, 9))\n' >"$work/hello.py"
out="$(bash "$launcher" "$work/hello.py" world 2>&1)"
rc=$?
[ "$rc" -eq 0 ] && ok || bad "trivial script exit $rc"
[ "$out" = "hello world True" ] && ok || bad "trivial script output: $out"

out="$(printf 'piped' | bash "$launcher" -c 'import sys; print(sys.stdin.read())' 2>&1)"
[ "$out" = "piped" ] && ok || bad "stdin is passed through: $out"

out="$(PATH=/nonexistent "$BASH" "$launcher" "$work/hello.py" 2>&1)"
rc=$?
[ "$rc" -eq 7 ] && ok || bad "no interpreter: want exit 7, got $rc"
case "$out" in *"no working Python"*Windows*python.org*) ok ;; *) bad "no interpreter hints: $out" ;; esac

echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
