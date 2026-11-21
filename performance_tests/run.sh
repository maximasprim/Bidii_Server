#!/usr/bin/env bash
# Runs the Staff Performance tests in isolation: in-memory SQLite, dummy Supabase,
# no AI, no SMTP. The environment is forced inside performance_tests/conftest.py,
# which aborts if anything real (e.g. a .env) is picked up. Run from the backend folder.
set -euo pipefail
cd "$(dirname "$0")/.."
echo "== Module ENABLED =="
PERF_TEST_FLAG=on python -m pytest performance_tests -q -p no:cacheprovider -W ignore "$@"
echo "== Module DISABLED (parity with the original app) =="
PERF_TEST_FLAG=off python -m pytest performance_tests/test_parity_flag_off.py -q -p no:cacheprovider -W ignore "$@"
