# The parser's blob proxy (docs/plans/parser-wrapper.md): squid with Basic auth on every request, behind its own egress rules.
# trixie, not bookworm: squid 5.7 resets plain-http uploads of ~10 MB and up to SeaweedFS; 6.x does not. Pin and track security updates.
# Build context: blob-sidecar/
FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends squid iptables util-linux python3 openssl tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY egress_rules.py start.py /app/
EXPOSE 3128
ENTRYPOINT ["tini", "--", "python3", "/app/start.py"]
