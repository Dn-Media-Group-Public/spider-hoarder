#!/bin/sh
# One browser session, run as the session's own uid by sessiond.
# In: HOME DISPLAY_NUM VNC_PORT WS_PORT MCP_PORT [PROXY_SERVER]; $HOME/vncpass, $HOME/hostrules
set -eu
HERE=$(dirname "$(readlink -f "$0")")
export DISPLAY=":$DISPLAY_NUM"
mkdir -p "$HOME/profile"  # $HOME/out is created by sessiond (group gw-collect)

Xvfb "$DISPLAY" -screen 0 1440x900x24 -nolisten tcp &
i=0; while [ ! -e "/tmp/.X11-unix/X$DISPLAY_NUM" ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i+1)); done

# VNC password comes from a 0600 file, never argv (/proc/*/cmdline is world-readable)
x11vnc -display "$DISPLAY" -passwdfile "$HOME/vncpass" -localhost -rfbport "$VNC_PORT" \
       -forever -shared -quiet -o "$HOME/x11vnc.log" &
websockify --web /usr/share/novnc "0.0.0.0:$WS_PORT" "127.0.0.1:$VNC_PORT" &

# Tor/I2P: Chromium talks to a local filtering SOCKS relay (blocklist), which forwards to the router
if [ -n "${UPSTREAM_SOCKS:-}" ]; then
  /venv/bin/python -m spider_hoarder_browser_sdk.session_server.session.filter_proxy "$FILTER_PORT" "${UPSTREAM_SOCKS%:*}" "${UPSTREAM_SOCKS#*:}" &
  PROXY_SERVER="socks5://127.0.0.1:$FILTER_PORT"
fi

ARGS='"--window-position=0,0","--window-size=1440,900","--disable-dev-shm-usage","--force-webrtc-ip-handling-policy=disable_non_proxied_udp"'
NAV_TIMEOUT=30000
if [ -n "${ALLOW_SUFFIX:-}" ]; then  # i2p: eepsites are http, and the first connect takes tens of seconds
  ARGS="$ARGS,\"--disable-features=HttpsUpgrades,HttpsFirstBalancedModeAutoEnable\""
  NAV_TIMEOUT=120000
fi
[ -n "${PROXY_SERVER:-}" ] && ARGS="$ARGS,\"--proxy-server=$PROXY_SERVER\""
[ -s "$HOME/hostrules" ] && ARGS="$ARGS,\"--host-rules=$(cat "$HOME/hostrules")\""

cat > "$HOME/toolserver.json" <<JSON
{
  "userDataDir": "$HOME/profile",
  "args": [$ARGS],
  "initScript": "$HERE/../toolserver/consent.js",
  "outDir": "$HOME/out",
  "inDir": "$HOME/in",
  "navTimeout": $NAV_TIMEOUT,
  "port": $MCP_PORT
}
JSON
exec /venv/bin/python -m spider_hoarder_browser_sdk.session_server.toolserver "$HOME/toolserver.json"
