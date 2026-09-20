FROM python:3.11-slim

ARG TARGETARCH
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONMALLOC=malloc

RUN case "${TARGETARCH:-amd64}" in amd64|arm64) ;; *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; esac \
    && apt-get update && apt-get install -y --no-install-recommends \
       ca-certificates \
       curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Build the commit-pinned worldwide airport/runway reference database into the
# image. Runtime monitoring performs local read-only SQLite lookups only.
RUN python /app/scripts/build_airport_database.py \
    && chmod 0444 /app/data/aviation/compiled/global_airports.sqlite3 \
    && chmod +x /app/scripts/railway-entrypoint.sh /app/scripts/planealerts \
    && ln -sf /app/scripts/planealerts /usr/local/bin/planealerts

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=4 \
    CMD curl -fsS http://127.0.0.1:${PORT:-8000}/health >/dev/null || exit 1

# Railway uses this entrypoint for required production configuration checks.
# Docker Compose overrides it with direct uvicorn startup for local self-hosting.
CMD ["/app/scripts/railway-entrypoint.sh"]
