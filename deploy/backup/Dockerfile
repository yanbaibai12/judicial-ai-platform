FROM alpine:3.20

RUN apk add --no-cache mariadb-client bash coreutils curl \
    && addgroup -g 10001 app && adduser -u 10001 -G app -D -H app

# mc（MinIO Client）按需安装；离线环境可预置到镜像仓库
ARG INSTALL_MC=true
RUN if [ "$INSTALL_MC" = "true" ]; then \
      curl -fsSL -o /usr/local/bin/mc https://dl.min.io/client/mc/release/linux-amd64/mc \
      && chmod +x /usr/local/bin/mc; \
    fi

COPY backup.sh /usr/local/bin/backup.sh
COPY restore.sh /usr/local/bin/restore.sh
RUN chmod +x /usr/local/bin/backup.sh /usr/local/bin/restore.sh

USER 10001:10001
VOLUME ["/backups"]
ENTRYPOINT ["/usr/local/bin/backup.sh"]
