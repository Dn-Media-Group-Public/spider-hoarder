# The OCR sidecar (docs/plans/parser-wrapper.md): stateless forwarder to the configured OpenAI-compatible profiles, own pinned egress.
# Build context: ocr-sidecar/
FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv iptables util-linux tini ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /venv && /venv/bin/pip install --no-cache-dir uvicorn starlette httpx google-auth requests \
 && useradd --system --no-create-home ocr
ENV PATH=/venv/bin:$PATH
COPY app.py egress_rules.py start.py /app/
EXPOSE 8080
ENTRYPOINT ["tini", "--", "python", "/app/start.py"]
