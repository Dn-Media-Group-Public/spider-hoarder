FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends tor iptables iproute2 util-linux \
 && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /entrypoint.sh
# root only for the entrypoint's iptables rules (NET_ADMIN); tor itself runs as debian-tor with no capabilities
EXPOSE 9040 5353
ENTRYPOINT ["/entrypoint.sh"]
