#!/usr/bin/env bash
# Runs every test suite, one pytest process per package area: the gateway picks its driver at import, so areas cannot share
# a process. Needs the in-tree packages installed in .venv (./deploy.sh does that, or pip install --no-deps ./sdk ./backends/*/).
# Usage: scripts/test.sh [-k expr ...]  (extra arguments go to every pytest)
cd "$(dirname "$0")/.."
PY=$PWD/.venv/bin/python
fail=0
# The tests need a real object store (s3fs's listing cache, its delete and the STS credentials behave differently from any fake): one
# throwaway SeaweedFS for all areas. Set TEST_BLOB_URL (s3://bucket) and TEST_BLOB_OPTIONS (JSON for fsspec) to use another store.
if [ -z "${TEST_BLOB_URL:-}" ]; then
  sw=$(mktemp -d)
  SEAWEEDFS_KEY=testkey SEAWEEDFS_SECRET=testsecret SEAWEEDFS_SIGNING_KEY=$(openssl rand -base64 32) $PY scripts/spider_hoarder.py seaweedfs-config "$sw" || exit 1
  cid=$(docker run -d --rm -p 127.0.0.1::8333 -v "$sw":/cfg:ro chrislusf/seaweedfs:4.53 server -s3 -s3.config=/cfg/s3.json \
        -s3.iam.config=/cfg/iam.json -s3.iam.readOnly=false -dir=/tmp) || exit 1
  trap 'docker rm -f "$cid" >/dev/null 2>&1; rm -rf "$sw"' EXIT
  for _ in $(seq 60); do
    echo "s3.bucket.create -name spider-hoarder-test" | docker exec -i "$cid" weed shell -master=localhost:9333 2>&1 | grep -q "created bucket\|already exists" && break
    sleep 1
  done
  export TEST_BLOB_URL=s3://spider-hoarder-test
  export TEST_BLOB_OPTIONS="{\"key\": \"testkey\", \"secret\": \"testsecret\", \"endpoint_url\": \"http://127.0.0.1:$(docker port "$cid" 8333/tcp | head -1 | sed 's/.*://')\"}"
fi
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
