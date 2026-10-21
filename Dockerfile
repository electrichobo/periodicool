# PeriodiCool container image.
# Runs as PUID:PGID (default 99:100 for Unraid), never as root.

FROM python:3.14-slim

LABEL org.opencontainers.image.title="PeriodiCool" \
      org.opencontainers.image.description="Self-hosted *arr-style manager for magazines" \
      org.opencontainers.image.source="https://github.com/electrichobo/periodicool" \
      org.opencontainers.image.licenses="GPL-3.0-only"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PERIODICOOL_CONFIG_DIR=/config \
    PERIODICOOL_LIBRARY_DIR=/library \
    PERIODICOOL_DOWNLOADS_DIR=/downloads \
    PERIODICOOL_HOST=0.0.0.0 \
    PERIODICOOL_PORT=8383 \
    PUID=99 \
    PGID=100 \
    UMASK=002 \
    TZ=UTC

# setpriv (util-linux) drops root after the entrypoint has prepared /config.
RUN command -v setpriv

WORKDIR /build
COPY requirements.lock pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install -c requirements.lock . \
    && cd / && rm -rf /build

COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/periodicool-entrypoint

WORKDIR /config
VOLUME ["/config"]
EXPOSE 8383

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PERIODICOOL_PORT', '8383') + '/health', timeout=5)"

ENTRYPOINT ["periodicool-entrypoint"]
CMD ["python", "-m", "periodicool"]
