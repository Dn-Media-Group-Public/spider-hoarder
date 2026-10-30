# android_tor: the Android gateway base image plus this backend's package. Built by scripts/ with
# `--build-context wheels=<dir of the wheels deploy.sh built>`.
FROM spider-hoarder-android-base:local
COPY --from=wheels / /tmp/wheels
RUN pip install --no-cache-dir --no-deps /tmp/wheels/android_tor-*.whl && rm -rf /tmp/wheels && chmod -R a+rX /venv/lib
