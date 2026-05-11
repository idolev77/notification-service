# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Notification Service - Application Image
# ---------------------------------------------------------------------------
# WHY a single image for api + worker + beat:
#   The same code/deps are needed by all three roles. The role is selected at
#   runtime via the container `command:` in docker-compose.yml. This keeps the
#   build cache warm and guarantees code parity between the API and workers.
# ---------------------------------------------------------------------------

FROM python:3.11-slim AS base

# WHY: deterministic, log-friendly Python runtime in containers.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100

# WHY: build-essential + libpq-dev are required by psycopg (Postgres driver).
#      curl is kept for container-level healthchecks.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
 && rm -rf /var/lib/apt/lists/*

# WHY: run as non-root to reduce blast radius of any RCE in deps.
RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

# WHY: copy requirements first to leverage Docker layer caching on code edits.
COPY requirements.txt ./
RUN pip install --upgrade pip \
 && pip install -r requirements.txt

# Copy source last so code changes don't bust the dependency layer.
COPY . .

RUN chown -R app:app /app
USER app

EXPOSE 8000

# Default command runs the API. Worker / beat override this in compose.
# WHY uvicorn (not gunicorn) by default: simpler local dev surface; for prod
# scale-out we'd front it with gunicorn+uvicorn workers behind a reverse proxy.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
