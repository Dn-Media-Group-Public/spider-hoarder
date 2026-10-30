#!/usr/bin/env bash
# Runs every test suite, one pytest process per package area: the gateway picks its driver at import, so areas cannot share
# a process. Needs the in-tree packages installed in .venv (./deploy.sh does that, or pip install --no-deps ./sdk ./backends/*/).
# Usage: scripts/test.sh [-k expr ...]  (extra arguments go to every pytest)
cd "$(dirname "$0")/.."
PY=$PWD/.venv/bin/python
fail=0
run() { echo "==> $*"; "$@" || fail=1; }
areas=(sdk backends/browser-sdk backends/android-sdk scripts)
for d in backends/browser_* backends/android_*; do [ -d "$d/tests" ] && areas+=("$d"); done
for a in "${areas[@]}"; do
  [ -d "$a" ] || continue
  # rc 5 = no tests collected: an area without tests is fine
  echo "==> pytest $a"
  $PY -m pytest -q "$a" "$@"; rc=$?
  [ "$rc" -eq 0 ] || [ "$rc" -eq 5 ] || fail=1
done
A=backends/android-sdk/spider_hoarder_android_sdk
run $PY $A/wrapper/test_reconcile.py
run $PY $A/slot/test_slot.py
(cd $A/slot/toolserver && run $PY test_android_toolserver.py)
exit $fail
