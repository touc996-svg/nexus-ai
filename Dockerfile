FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production

# Pick up Debian security fixes published after the base image was built (e.g. libzstd1 CVE-2026-124851).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# setuptools and wheel are build tools the running app does not use; their vendored copies carried HIGH CVEs
# (jaraco.context, wheel) in the Trivy scan, so they are removed from the runtime image.
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && pip uninstall -y setuptools wheel \
    && groupadd --system nexus \
    && useradd --system --gid nexus --home-dir /app nexus

COPY --chown=nexus:nexus app ./app
RUN chown -R nexus:nexus /app

USER nexus
EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/health/live', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
