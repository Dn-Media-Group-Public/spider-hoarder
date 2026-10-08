FROM node:22-bookworm-slim

ARG PLAYWRIGHT_MCP_VERSION=0.0.83
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH=/venv/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip ca-certificates curl tini iptables util-linux \
      xvfb x11vnc novnc websockify fonts-noto fonts-noto-cjk fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

# @playwright/mcp (pinned; flags/config keys change between releases) + the Chromium it expects
RUN npm install -g @playwright/mcp@${PLAYWRIGHT_MCP_VERSION} \
 && cd "$(npm root -g)/@playwright/mcp" \
 && npx --no-install playwright install --with-deps chromium \
 && chmod -R a+rX /ms-playwright \
 && rm -rf /var/lib/apt/lists/* /root/.npm

WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /venv && pip install --no-cache-dir -r requirements.txt

# Cookie-consent network blocklist, fetched once at build time (no runtime
# network dependency): Fanboy's Cookiemonster list.
RUN python -c "import requests; open('cmp_blocklist.txt', 'wb').write(requests.get('https://secure.fanboy.co.nz/fanboy-cookiemonster.txt', headers={'User-Agent': 'Mozilla/5.0'}).content)"

COPY . .
# /data: blob storage (gateway uid 1000). /home, /run: sessiond's per-session homes and socket.
RUN chmod +x session-group.sh entrypoint.sh && mkdir -p /data && chown 1000:1000 /data

EXPOSE 8000
ENTRYPOINT ["tini", "--", "/app/entrypoint.sh"]
