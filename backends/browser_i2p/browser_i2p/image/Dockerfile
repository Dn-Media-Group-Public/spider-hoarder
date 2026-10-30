# browser_i2p: the browser base image plus this backend's package. Built by scripts/ with
# `--build-context wheels=<dir of the wheels deploy.sh built>`.
FROM spider-hoarder-browser-base:local
COPY --from=wheels / /tmp/wheels
RUN pip install --no-cache-dir --no-deps /tmp/wheels/browser_i2p-*.whl && rm -rf /tmp/wheels && chmod -R a+rX /venv/lib
