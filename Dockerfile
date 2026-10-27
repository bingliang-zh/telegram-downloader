ARG TDL_VERSION=v0.20.2

FROM golang:1.25-bookworm AS tdl-builder

ARG TDL_VERSION
ENV CGO_ENABLED=0
RUN GOBIN=/out go install github.com/iyear/tdl@${TDL_VERSION}

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_DATA_DIR=/data \
    DOWNLOAD_DIR=/downloads \
    TDL_STORAGE_PATH=/data/tdl \
    TDL_NS=default \
    TZ=Asia/Shanghai

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY telegram_downloader ./telegram_downloader
COPY docker-entrypoint.sh /usr/local/bin/telegram-downloader-entrypoint
COPY --from=tdl-builder /out/tdl /usr/local/bin/tdl

RUN pip install --no-cache-dir . \
    && chmod +x /usr/local/bin/telegram-downloader-entrypoint

RUN mkdir -p /data /downloads \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app /data /downloads

USER appuser

ENTRYPOINT ["/usr/local/bin/telegram-downloader-entrypoint"]
CMD ["daemon"]
