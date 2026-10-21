#!/usr/bin/env bash
# Run live eval scenarios against the fixture repos. See evals-harness/README.md.
#
#   scripts/eval-live.sh --owner OWNER --repo OWNER/x-fixture [--companion OWNER/x-fixture-companion] \
#       --skill new --scenario N1 [run_live.py flags such as --runs 3 --cap 6 --dry-run]
#
# --owner, --repo and --companion are exported as NOT_ME_FIXTURE_OWNER, NOT_ME_FIXTURE_REPO and
# NOT_ME_FIXTURE_COMPANION; everything else is passed to run_live.py unchanged.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
passthrough=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --owner|--repo|--companion)
      if [ "$#" -lt 2 ]; then
        echo "eval-live: $1 needs a value" >&2
        exit 2
      fi
      case "$1" in
        --owner) export NOT_ME_FIXTURE_OWNER="$2" ;;
        --repo) export NOT_ME_FIXTURE_REPO="$2" ;;
        --companion) export NOT_ME_FIXTURE_COMPANION="$2" ;;
      esac
      shift 2
      ;;
    -h|--help)
      sed -n '2,9p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      passthrough+=("$1")
      shift
      ;;
  esac
done

if [ -z "${NOT_ME_FIXTURE_OWNER:-}" ] || [ -z "${NOT_ME_FIXTURE_REPO:-}" ]; then
  echo "eval-live: --owner and --repo are required (or NOT_ME_FIXTURE_OWNER and NOT_ME_FIXTURE_REPO)" >&2
  exit 2
fi

python_bin="${PYTHON:-python3}"
exec "$python_bin" "$root/evals-harness/run_live.py" ${passthrough[@]+"${passthrough[@]}"}
