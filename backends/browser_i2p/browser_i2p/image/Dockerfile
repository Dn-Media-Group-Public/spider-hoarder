# browser_i2p: the browser gateway base image plus this backend's package. Build context: the repo root
# (docker build -f backends/browser_i2p/browser_i2p/image/Dockerfile .).
FROM spider-hoarder-browser-base:local
COPY backends/browser_i2p/ /src/browser_i2p/
RUN pip install --no-cache-dir --no-deps /src/browser_i2p && rm -rf /src && chmod -R a+rX /venv/lib
