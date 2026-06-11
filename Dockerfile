# syntax=docker/dockerfile:1.7
# ---- Builder ---------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install build tools needed by some wheels (cffi, cryptography fallback).
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# Install dependencies into a dedicated prefix that we copy into the
# runtime image. This keeps the final image free of compilers.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --prefix=/install .

# ---- Runtime --------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/install/bin:${PATH}" \
    PYTHONPATH="/install/lib/python3.11/site-packages"

# Create a non-root user so we never run trade orders as root.
RUN useradd --create-home --shell /bin/bash botuser
WORKDIR /home/botuser/app

COPY --from=builder /install /install
COPY --chown=botuser:botuser src ./src
COPY --chown=botuser:botuser pyproject.toml README.md ./

# Persist database and logs on a mounted volume.
RUN mkdir -p data logs && chown -R botuser:botuser data logs

USER botuser

# Default DB inside the container goes into ./data which is intended to
# be backed by a volume. Override DATABASE_URL via env if desired.
ENV DATABASE_URL=sqlite+aiosqlite:////home/botuser/app/data/newstrategybot.db \
    LOG_FILE=/home/botuser/app/logs/bot.log

CMD ["python", "-m", "src.main"]
