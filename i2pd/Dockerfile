FROM alpine:3.20
RUN apk add --no-cache i2pd
COPY entrypoint.sh /entrypoint.sh
USER i2pd
ENTRYPOINT ["/entrypoint.sh"]
