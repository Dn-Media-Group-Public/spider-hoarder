FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends tor util-linux \
 && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /entrypoint.sh
# runs as debian-tor with no capabilities and no privileges to give up (the browsers only use its SocksPorts)
EXPOSE 9050-9300
USER debian-tor
ENTRYPOINT ["/entrypoint.sh"]
