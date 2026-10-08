#!/bin/sh
# One browser session, run as the session's own uid by sessiond.
# In: HOME DISPLAY_NUM VNC_PORT WS_PORT MCP_PORT [PROXY_SERVER]; $HOME/vncpass, $HOME/hostrules
set -eu
export DISPLAY=":$DISPLAY_NUM"
mkdir -p "$HOME/profile" "$HOME/output"

Xvfb "$DISPLAY" -screen 0 1440x900x24 -nolisten tcp &
i=0; while [ ! -e "/tmp/.X11-unix/X$DISPLAY_NUM" ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i+1)); done

# VNC password comes from a 0600 file, never argv (/proc/*/cmdline is world-readable)
x11vnc -display "$DISPLAY" -passwdfile "$HOME/vncpass" -localhost -rfbport "$VNC_PORT" \
       -forever -shared -quiet -o "$HOME/x11vnc.log" &
websockify --web /usr/share/novnc "0.0.0.0:$WS_PORT" "localhost:$VNC_PORT" &

# Tor: Chromium talks to a local filtering SOCKS relay (blocklist), which forwards to Tor
if [ -n "${UPSTREAM_SOCKS:-}" ]; then
  /venv/bin/python /app/filter_proxy.py "$FILTER_PORT" "${UPSTREAM_SOCKS%:*}" "${UPSTREAM_SOCKS#*:}" &
  PROXY_SERVER="socks5://127.0.0.1:$FILTER_PORT"
fi

ARGS='"--window-position=0,0","--window-size=1440,900","--disable-dev-shm-usage","--force-webrtc-ip-handling-policy=disable_non_proxied_udp"'
[ -n "${PROXY_SERVER:-}" ] && ARGS="$ARGS,\"--proxy-server=$PROXY_SERVER\""
[ -s "$HOME/hostrules" ] && ARGS="$ARGS,\"--host-rules=$(cat "$HOME/hostrules")\""

cat > "$HOME/mcp.json" <<JSON
{
  "browser": {
    "browserName": "chromium",
    "userDataDir": "$HOME/profile",
    "launchOptions": {"headless": false, "chromiumSandbox": false, "args": [$ARGS]},
    "contextOptions": {"viewport": null},
    "initScript": ["/app/consent.js"]
  },
  "server": {"host": "127.0.0.1", "port": $MCP_PORT, "allowedHosts": ["mcp-upstream"]},
  "outputDir": "$HOME/output"
}
JSON
exec playwright-mcp --config "$HOME/mcp.json" --caps pdf
