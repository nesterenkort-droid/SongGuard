# Single shared image for web / worker / bot / migrate / seed.
# They differ only by the command compose runs — one build, shared layers.
# (When the worker gains ffmpeg + a JRE for Panako in M5, this may split.)
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# curl is used by the web container's healthcheck.
# java, ffmpeg, and sox are used by Panako for audio decoding and fingerprinting.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl openjdk-17-jre-headless ffmpeg sox \
    && mkdir -p /app/bin \
    && curl -L -o /app/bin/panako.jar https://github.com/JorenSix/Panako/releases/download/joss/Panako-2.1-all.jar \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (cached until the lockfile changes).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

# Put the project venv on PATH so uvicorn/alembic/arq/python resolve to it.
ENV PATH="/app/.venv/bin:$PATH"

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
