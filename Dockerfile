# One image for all three entrypoints:
#   dashboard (default):  uvicorn dashboard.app:app
#   ingest job:           python -m alex311.ingest incremental
#   health job:           python -m alex311.health
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY dashboard ./dashboard
COPY docs/data ./docs/data
RUN pip install --no-cache-dir ".[gcs]"

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
# Cloud Run terminates TLS and forwards plain HTTP. Trust its X-Forwarded-*
# headers (it is the only way in), so request.url.scheme is "https" and the
# session cookie is set Secure.
CMD ["uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
