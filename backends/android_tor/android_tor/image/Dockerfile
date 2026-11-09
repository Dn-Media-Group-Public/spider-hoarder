# android_tor: the Android gateway base image plus this backend's package. Build context: the repo root
# (docker build -f backends/android_tor/android_tor/image/Dockerfile .).
FROM spider-hoarder-android-base:local
COPY backends/android_tor/ /src/android_tor/
RUN pip install --no-cache-dir --no-deps /src/android_tor && rm -rf /src && chmod -R a+rX /venv/lib
