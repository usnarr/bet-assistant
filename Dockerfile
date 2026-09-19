# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.12.15 AS uv
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY --from=builder /app/.venv ./.venv
COPY --from=builder /app/migrations ./migrations
COPY --from=builder /app/alembic.ini ./

# Runtime containers do not install packages. Removing pip/ensurepip also removes
# their vendored build libraries from the attack surface and vulnerability scan.
RUN rm -rf \
      /usr/local/lib/python3.13/ensurepip \
      /usr/local/lib/python3.13/site-packages/pip \
      /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
      /usr/local/bin/pip \
      /usr/local/bin/pip3 \
      /usr/local/bin/pip3.13 \
    && useradd --create-home --uid 10001 tennis \
    && chown -R tennis:tennis /app

USER tennis

EXPOSE 8000
CMD ["uvicorn", "tennis_engine.serving.api:app", "--host", "0.0.0.0", "--port", "8000"]
