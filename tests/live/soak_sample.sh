#!/usr/bin/env bash
# Host-side sampler for soak_android.py (docs/implemented-plans/android-kvm-bringup.md stage 6): every INTERVAL seconds one TSV line per
# android slot (container id, cgroup memory now/peak, qemu RSS, /tmp and overlay upper size, process and zombie counts, OOM flag)
# plus gateway memory, leaked volumes/containers, and new OOM lines in dmesg. Usage: ./soak_sample.sh [interval=60] > samples.tsv
set -u
INTERVAL=${1:-60}
echo -e "time\tslot\tcid\tmem_MB\tpeak_MB\tqemu_rss_MB\ttmp_MB\tupper_MB\tprocs\tzombies\toom\tgateways_MB\tvolumes\tcontainers"
while :; do
  vols=$(docker volume ls -q | grep -c 'android-.*-\(avd\|state\)$')
  ctrs=$(docker ps -aq --filter label=spider-hoarder.android-slot | wc -l)
  gw=$(docker stats --no-stream --format '{{.MemUsage}}' $(docker ps -q -f name=research-android) 2>/dev/null | awk '{s+=$1} END {printf "%.0f", s}')
  for n in $(docker ps --format '{{.Names}}' --filter label=spider-hoarder.android-slot); do
    cid=$(docker inspect -f '{{printf "%.8s" .Id}}' "$n")
    oom=$(docker inspect -f '{{.State.OOMKilled}}' "$n")
    docker exec "$n" sh -c '
      cg=/sys/fs/cgroup
      mem=$(( $(cat $cg/memory.current) / 1048576 )); peak=$(( $(cat $cg/memory.peak 2>/dev/null || echo 0) / 1048576 ))
      rss=$(ps -eo rss,comm | awk "\$2 ~ /^qemu-system/ {s+=\$1} END {printf \"%d\", s/1024}")
      tmp=$(du -sm /tmp 2>/dev/null | cut -f1); upper=$(du -sm /avd/slot.avd 2>/dev/null | cut -f1)
      procs=$(ps -e | wc -l); z=$(ps -eo stat | grep -c "^Z")
      echo "$mem\t$peak\t$rss\t$tmp\t$upper\t$procs\t$z"' 2>/dev/null | { read -r line || line="?\t?\t?\t?\t?\t?\t?"
      echo -e "$(date +%T)\t$n\t$cid\t$line\t$oom\t$gw\t$vols\t$ctrs"; }
  done
  dmesg 2>/dev/null | grep -i -E "out of memory|oom-kill" | tail -2 | sed 's/^/DMESG\t/'
  sleep "$INTERVAL"
done
