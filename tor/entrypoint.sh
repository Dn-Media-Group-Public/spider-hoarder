#!/bin/sh
# One SocksPort per session slot (+1 for the gateway's own requests). Tor isolates
# streams from different SocksPorts, so different sessions/users never share circuits.
# Port numbers must match mcp-research/ports.py: 9050 + pool index; POOL_SIZE = MAX_SESSIONS_TOTAL + 2.
set -eu
POOL_SIZE=$(( ${MAX_SESSIONS_TOTAL:-18} + 2 ))
{
  i=0; while [ "$i" -le "$POOL_SIZE" ]; do echo "SocksPort 0.0.0.0:$((9050+i))"; i=$((i+1)); done
  echo "SocksPolicy accept 10.0.0.0/8"
  echo "SocksPolicy accept 172.16.0.0/12"
  echo "SocksPolicy accept 192.168.0.0/16"
  echo "SocksPolicy reject *"
  echo "DataDirectory /var/lib/tor"
  echo "Log notice stdout"
} > /tmp/torrc
exec tor -f /tmp/torrc
