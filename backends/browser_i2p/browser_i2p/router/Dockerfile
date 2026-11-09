FROM alpine:3.20
RUN apk add --no-cache i2pd
COPY backends/browser_i2p/browser_i2p/router/entrypoint.sh /entrypoint.sh
USER i2pd
ENTRYPOINT ["/entrypoint.sh"]
