FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends tor \
 && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /entrypoint.sh
USER debian-tor
EXPOSE 9050-9100
ENTRYPOINT ["/entrypoint.sh"]
