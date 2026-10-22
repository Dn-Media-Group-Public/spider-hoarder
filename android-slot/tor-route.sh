#!/bin/sh
# Points the default route at the tor task (tasks.tor = the task's own address, not the service VIP). Run by entrypoint.sh
# at slot start and, via `docker exec`, by the wrapper on every pass: the tor task gets a new address whenever it is
# replaced, and a route to the old one leaves the slot without any network. Exit: 0 route already right (or tor not
# resolvable yet), 10 route changed. Output is not for the wrapper to parse.
set -eu
ip=$(getent ahostsv4 tasks.tor | awk 'NR==1{print $1}')
case "$ip" in
  *[!0-9.]*|"") exit 0 ;;
esac
ip route show default | grep -q " via $ip " && exit 0
ip route replace default via "$ip"
exit 10
