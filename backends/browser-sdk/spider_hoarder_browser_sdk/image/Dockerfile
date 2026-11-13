# Base image of every browser backend: Chromium, Xvfb/VNC, the SDK and the browser-sdk (session daemon, tool server, driver).
# A backend image is FROM this plus its own package (backends/browser_*/image/Dockerfile).
# Build context: the repo root (docker build -f backends/browser-sdk/spider_hoarder_browser_sdk/image/Dockerfile .).
FROM debian:bookworm-slim

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH=/venv/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip ca-certificates curl tini iptables util-linux \
      xvfb x11vnc novnc websockify fonts-noto fonts-noto-cjk fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

# Chromium for the toolserver. playwright is pinned like in browser-sdk's pyproject, and installed first so that a code
# change does not redo the Chromium download.
RUN python3 -m venv /venv && pip install --no-cache-dir playwright==1.63.0 requests \
 && playwright install --with-deps chromium \
 && chmod -R a+rX /ms-playwright \
 && rm -rf /var/lib/apt/lists/*

# Cookie-consent network blocklist, fetched once at build time (no runtime
# network dependency): Fanboy's Cookiemonster list.
RUN python -c "import requests; open('/opt/cmp_blocklist.txt', 'wb').write(requests.get('https://secure.fanboy.co.nz/fanboy-cookiemonster.txt', headers={'User-Agent': 'Mozilla/5.0'}).content)"
ENV CMP_BLOCKLIST_PATH=/opt/cmp_blocklist.txt

# The SDK packages (third-party dependencies come from PyPI, ours from the source tree)
COPY sdk/ /src/sdk/
COPY backends/browser-sdk/ /src/browser-sdk/
# GCS support is a separate pip step: sdk/pyproject.toml explains the protobuf pin that stops one resolution
RUN pip install --no-cache-dir "/src/sdk[s3]" /src/browser-sdk \
 && pip install --no-cache-dir --no-deps "boto3==$(python -c 'import botocore;print(botocore.__version__)')" s3transfer \
 && pip install --no-cache-dir gcsfs google-auth \
 && rm -rf /src && chmod -R a+rX /venv/lib \
 && chmod +x /venv/lib/python*/site-packages/spider_hoarder_browser_sdk/session_server/session/session-group.sh
# gw-collect (gid 1500): the gateway is added at start (start.py setpriv); session out/ dirs belong to it.
# /home, /run: sessiond's per-session homes and socket.
RUN groupadd -g 1500 gw-collect

# /app/entrypoint.sh: the same path in every family, so the dev overlay can wrap it (localhost-is-host.sh)
RUN mkdir -p /app && printf '#!/bin/sh\nexec python -m spider_hoarder_browser_sdk.start\n' > /app/entrypoint.sh && chmod +x /app/entrypoint.sh
WORKDIR /app
EXPOSE 8000
ENTRYPOINT ["tini", "--", "/app/entrypoint.sh"]
