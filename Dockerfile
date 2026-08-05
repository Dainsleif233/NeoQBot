# syntax=docker/dockerfile:1
FROM python:3.12-slim

ARG INSTALL_FEISHU_CLI=false
ARG FEISHU_CLI_PACKAGE=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NEOQBOT_CONFIG=/app/data/config.yaml

WORKDIR /app

RUN if [ "$INSTALL_FEISHU_CLI" = "true" ]; then \
      test -n "$FEISHU_CLI_PACKAGE"; \
      apt-get update; \
      apt-get install -y --no-install-recommends nodejs npm ca-certificates; \
      npm install -g "$FEISHU_CLI_PACKAGE"; \
      rm -rf /var/lib/apt/lists/*; \
    fi

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . && useradd --create-home --uid 10001 neoqbot && mkdir -p /app/data && chown -R neoqbot:neoqbot /app

USER neoqbot
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1

CMD ["neoqbot", "serve"]
