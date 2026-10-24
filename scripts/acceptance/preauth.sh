#!/usr/bin/env bash
# Pre-authorization acceptance checks: everything that can be verified without
# Google credentials. Prints a PASS/FAIL/SKIP table and exits 1 if any check
# fails. Command output goes to log files, never to the table.
#
# It never runs commands that read the secret store or call Google
# (googlemcp auth, secret, token, doctor, serve), and it unsets
# GOOGLEMCP_LIVE so the live test suite stays skipped.
#
# Usage: scripts/acceptance/preauth.sh [--no-terraform] [--no-ci]
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 2

RUN_TERRAFORM=1
RUN_CI=1
for arg in "$@"; do
  case "$arg" in
    --no-terraform) RUN_TERRAFORM=0 ;;
    --no-ci) RUN_CI=0 ;;
    -h | --help)
      sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

unset GOOGLEMCP_LIVE
LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/googlemcp-preauth.XXXXXX")"
ROWS=()
FAILED=0

record() {
  ROWS+=("$1|$2|$3")
  if [ "$2" = "FAIL" ]; then
    FAILED=1
  fi
}

# run_check NAME COMMAND [ARGS...]: PASS when the command exits 0.
run_check() {
  local name="$1"
  shift
  local log
  log="$LOG_DIR/$(printf '%s' "$name" | tr -c 'a-zA-Z0-9' '_').log"
  if "$@" >"$log" 2>&1; then
    record "$name" PASS "$(last_line "$log")"
  else
    record "$name" FAIL "see $log"
  fi
}

# Last non-empty log line without ANSI colors, trimmed for the table.
last_line() {
  local line
  line="$(tr -d '\033' <"$1" | sed -e 's/\[[0-9;]*m//g' -e 's/[[:space:]]*$//' | grep -v '^$' | tail -n 1)"
  printf '%s' "${line:-ok}" | cut -c1-70
}

have() {
  command -v "$1" >/dev/null 2>&1
}

cli_smoke() {
  uv run googlemcp --help >/dev/null &&
    uv run googlemcp products >/dev/null &&
    uv run googlemcp scopes
}

ci_status() {
  gh run list --branch main --limit 1 --json status,conclusion \
    --jq '.[0] | "\(.status) \(.conclusion)"' 2>/dev/null
}

if ! have uv; then
  echo "uv is required: https://docs.astral.sh/uv/" >&2
  exit 2
fi

run_check "dependencies (uv sync --frozen)" uv sync --frozen
run_check "unit tests (pytest)" uv run pytest -p no:cacheprovider
run_check "lint (ruff check)" uv run ruff check .
run_check "format (ruff format --check)" uv run ruff format --check .
run_check "types (mypy)" uv run mypy

run_check "inventory current" uv run python scripts/gen_inventory.py --check

# The server core is part of main, so an import failure is a real failure.
run_check "cli smoke (help, products, scopes)" cli_smoke

if [ "$RUN_TERRAFORM" -eq 0 ]; then
  record "terraform verify" SKIP "--no-terraform"
elif ! have terraform; then
  record "terraform verify" SKIP "terraform not installed"
else
  run_check "terraform verify" infra/terraform/scripts/verify.sh
fi

if have gitleaks; then
  run_check "secret scan (gitleaks)" gitleaks git --redact --no-banner .
else
  record "secret scan (gitleaks)" SKIP "gitleaks not installed"
fi

if [ "$RUN_CI" -eq 0 ]; then
  record "ci on main" SKIP "--no-ci"
elif ! have gh; then
  record "ci on main" SKIP "gh not installed"
else
  status="$(ci_status || true)"
  case "$status" in
    "completed success") record "ci on main" PASS "latest run: success" ;;
    "" | "null null") record "ci on main" SKIP "no CI runs found or gh not signed in" ;;
    completed*) record "ci on main" FAIL "latest run: ${status#completed }" ;;
    *) record "ci on main" SKIP "latest run still ${status%% *}" ;;
  esac
fi

printf '\n%-36s %-5s %s\n' "CHECK" "RESULT" "DETAIL"
for row in "${ROWS[@]}"; do
  IFS='|' read -r name result detail <<<"$row"
  printf '%-36s %-5s %s\n' "$name" "$result" "$detail"
done
printf '\nlogs: %s\n' "$LOG_DIR"

if [ "$FAILED" -ne 0 ]; then
  echo "preauth: FAIL"
  exit 1
fi
echo "preauth: PASS"
