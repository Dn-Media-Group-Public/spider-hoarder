# browser_clear: the browser gateway base image plus this backend's package. Build context: the repo root
# (docker build -f backends/browser_clear/browser_clear/image/Dockerfile .).
FROM spider-hoarder-browser-base:local
COPY backends/browser_clear/ /src/browser_clear/
RUN pip install --no-cache-dir --no-deps /src/browser_clear && rm -rf /src && chmod -R a+rX /venv/lib
