FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PRIVATE_AI_OPS_ENABLED=true \
    AI_OPS_DATA_DIR=/data/ai_ops \
    AI_OPS_SCHEDULER_ENABLED=false \
    AI_OPS_REAL_PROVIDERS_ENABLED=false \
    AI_OPS_PHASE1_PROBE_ENABLED=true

WORKDIR /app
COPY app/__init__.py /app/app/__init__.py
COPY app/private_ops/ /app/app/private_ops/

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/ready", timeout=3).close()' || exit 1

CMD ["python", "-m", "app.private_ops.service"]
