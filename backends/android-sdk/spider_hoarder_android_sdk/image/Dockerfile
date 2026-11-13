# Gateway base image of the Android backends: the SDK and the android-sdk, no Chromium. The slots are other containers
# (../slot). A backend image is FROM this plus its own package (backends/android_*/image/Dockerfile).
# Build context: the repo root (docker build -f backends/android-sdk/spider_hoarder_android_sdk/image/Dockerfile .).
FROM debian:bookworm-slim

ENV PATH=/venv/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates tini util-linux \
 && rm -rf /var/lib/apt/lists/*

COPY sdk/ /src/sdk/
COPY backends/android-sdk/ /src/android-sdk/
# GCS support is a separate pip step: sdk/pyproject.toml explains the protobuf pin that stops one resolution
RUN python3 -m venv /venv \
 && pip install --no-cache-dir "/src/sdk[s3]" /src/android-sdk \
 && pip install --no-cache-dir --no-deps "boto3==$(python -c 'import botocore;print(botocore.__version__)')" s3transfer \
 && pip install --no-cache-dir gcsfs google-auth \
 && rm -rf /src && chmod -R a+rX /venv/lib
# gw-collect (gid 1500): the gateway's supplementary group at start.
RUN groupadd -g 1500 gw-collect
COPY backends/android-sdk/spider_hoarder_android_sdk/image/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["tini", "--", "/app/entrypoint.sh"]
