FROM quay.io/oauth2-proxy/oauth2-proxy:v7.15.5-alpine
USER root
RUN apk add --no-cache jq curl && mkdir /state && chown 65532 /state
COPY entrypoint.sh /entrypoint.sh
USER 65532
ENTRYPOINT ["/entrypoint.sh"]
