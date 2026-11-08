FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system nexus \
    && useradd --system --gid nexus --home-dir /app nexus

COPY --chown=nexus:nexus app ./app
RUN chown -R nexus:nexus /app

USER nexus
EXPOSE 8010

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/health/live', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
