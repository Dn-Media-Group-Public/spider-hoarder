FROM alpine:3.20
RUN apk add --no-cache i2pd nginx su-exec
COPY nginx.conf /etc/nginx/nginx.conf
COPY entrypoint.sh /entrypoint.sh
EXPOSE 80
ENTRYPOINT ["/entrypoint.sh"]
