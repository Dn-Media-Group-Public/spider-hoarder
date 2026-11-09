FROM alpine:3.20
RUN apk add --no-cache i2pd nginx su-exec
COPY backends/browser_i2p/browser_i2p/seed/nginx.conf /etc/nginx/nginx.conf
COPY backends/browser_i2p/browser_i2p/seed/entrypoint.sh /entrypoint.sh
EXPOSE 80
ENTRYPOINT ["/entrypoint.sh"]
