# Built only by the dedicated credential-free sandbox workflow. No application
# source or secrets are baked into the image; the pinned Git archive is mounted
# read-only for each individual investigation operation.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt requirements.lock /build/
RUN pip install --no-cache-dir -r /build/requirements.txt 'pytest>=8,<9' 'pytest-asyncio>=0.24,<1'
USER 65534:65534
WORKDIR /work
