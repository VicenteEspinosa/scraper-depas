# Built natively on the Oracle ARM (Ampere A1) box by `docker compose build`;
# there is no registry. Mirrors local dev (uv-managed, editable) so
# store.MIGRATIONS_DIR (= <repo>/migrations, one parent up from depas/store.py)
# resolves to /app/migrations.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependency layer — cached unless pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# supercronic — container-friendly cron for the depas-cron sidecar (logs to
# stdout, no daemon). arm64 only: the deploy box is an Ampere A1, built natively.
ENV SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.2.46/supercronic-linux-arm64 \
    SUPERCRONIC_SHA1SUM=639ab81a72771990790df7ee87d9acfe88e5fa83
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSLo /usr/local/bin/supercronic "$SUPERCRONIC_URL" \
 && echo "${SUPERCRONIC_SHA1SUM}  /usr/local/bin/supercronic" | sha1sum -c - \
 && chmod +x /usr/local/bin/supercronic \
 && rm -rf /var/lib/apt/lists/*
COPY deploy/crontab /app/crontab

# App layer.
COPY . .
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

CMD ["supercronic", "/app/crontab"]
