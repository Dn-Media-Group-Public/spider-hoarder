FROM debian:bookworm-slim

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH=/venv/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip ca-certificates curl tini iptables util-linux unar \
      xvfb x11vnc novnc websockify fonts-noto fonts-noto-cjk fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /venv && pip install --no-cache-dir -r requirements.txt

# Chromium for the toolserver (playwright pinned in requirements.txt)
RUN playwright install --with-deps chromium \
 && chmod -R a+rX /ms-playwright \
 && rm -rf /var/lib/apt/lists/*

# Cookie-consent network blocklist, fetched once at build time (no runtime
# network dependency): Fanboy's Cookiemonster list.
RUN python -c "import requests; open('/opt/cmp_blocklist.txt', 'wb').write(requests.get('https://secure.fanboy.co.nz/fanboy-cookiemonster.txt', headers={'User-Agent': 'Mozilla/5.0'}).content)"
ENV CMP_BLOCKLIST_PATH=/opt/cmp_blocklist.txt

COPY . .
RUN pip install --no-cache-dir . && rm -rf mcp_research build *.egg-info && chmod -R a+rX /venv/lib
# gw-collect (gid 1500): the gateway is added at start (entrypoint setpriv); session out/ dirs belong to it.
# /data: blob storage (gateway uid 1000). /home, /run: sessiond's per-session homes and socket.
RUN groupadd -g 1500 gw-collect && chmod +x entrypoint.sh && chmod +x /venv/lib/python*/site-packages/mcp_research/session_server/session/session-group.sh && mkdir -p /data && chown 1000:1000 /data

EXPOSE 8000
ENTRYPOINT ["tini", "--", "/app/entrypoint.sh"]
