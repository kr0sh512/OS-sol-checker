# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.5 AS uv
FROM python:3.12.10-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 checker && useradd --uid 10001 --gid 10001 --no-create-home checker
WORKDIR /opt/checker
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY migrations ./migrations
COPY deploy/container_entrypoint.py ./deploy/container_entrypoint.py
RUN uv sync --frozen --no-dev --no-editable \
    && mkdir -p /var/lib/checker/catalog \
    && chown 10001:10001 /var/lib/checker/catalog
ENV PATH="/opt/checker/.venv/bin:$PATH" PYTHONPATH=/opt/checker/src \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["/opt/checker/.venv/bin/python", "/opt/checker/deploy/container_entrypoint.py"]
CMD ["checker-web"]
