FROM python:3.13-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gcc g++ \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev


FROM python:3.13-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TOOLS_WORKSPACE_ROOT=/app \
    HF_HOME=/data/hf \
    XDG_CACHE_HOME=/data/cache \
    PATH="/app/.venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        nginx \
        supervisor \
        libgomp1 \
        ca-certificates \
        bash \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY . /app

RUN chmod +x /app/docker/runtime/entrypoint.sh \
    && mkdir -p /app/generated /data/hf /data/cache /var/log/supervisor

EXPOSE 8080

ENTRYPOINT ["/app/docker/runtime/entrypoint.sh"]
