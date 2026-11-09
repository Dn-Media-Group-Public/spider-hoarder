# android_clear: the Android gateway base image plus this backend's package. Build context: the repo root
# (docker build -f backends/android_clear/android_clear/image/Dockerfile .).
FROM spider-hoarder-android-base:local
COPY backends/android_clear/ /src/android_clear/
RUN pip install --no-cache-dir --no-deps /src/android_clear && rm -rf /src && chmod -R a+rX /venv/lib
