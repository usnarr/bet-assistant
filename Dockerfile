# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.12.15 AS uv
FROM python:3.13-slim

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
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --create-home --uid 10001 tennis && chown -R tennis:tennis /app
USER tennis

EXPOSE 8000
CMD ["uvicorn", "tennis_engine.serving.api:app", "--host", "0.0.0.0", "--port", "8000"]
