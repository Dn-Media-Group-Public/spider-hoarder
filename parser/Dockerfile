# The parser container (docs/plans/parser-wrapper.md): the wrapper (own uid, keeps CAP_SETUID/SETGID/CHOWN/KILL) and the sandboxed parser
# processes it forks as pool uids. No egress of its own: its networks hold only the gateways, the blob proxy and the OCR sidecar.
# Build context: the repo root (docker build -f parser/Dockerfile .). GCS support is a separate pip step (sdk/pyproject.toml explains the pin).
FROM debian:bookworm-slim
ENV PATH=/venv/bin:$PATH
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates tini util-linux unar libimage-exiftool-perl ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /venv \
 && groupadd -g 900 wrapper && useradd -u 900 -g 900 --no-create-home --shell /usr/sbin/nologin wrapper \
 && mkdir /scratch && chown wrapper:wrapper /scratch
COPY parser/ /src/parser/
RUN pip install --no-cache-dir "/src/parser[s3]" \
 && pip install --no-cache-dir gcsfs google-auth \
 && rm -rf /src && chmod -R a+rX /venv
# the scratch area is ephemeral: an anonymous volume of the task (never a named one), so a restart deletes what a crash left
VOLUME /scratch
COPY parser/entrypoint.sh /app/entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["tini", "--", "/app/entrypoint.sh"]
