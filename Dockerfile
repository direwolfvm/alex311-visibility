# One image for all three entrypoints:
#   dashboard (default):  uvicorn dashboard.app:app
#   ingest job:           python -m alex311.ingest incremental
#   health job:           python -m alex311.health
FROM python:3.14.7-slim

WORKDIR /app
# Dependencies come from uv.lock, the same versions CI tests and audits, not
# whatever pip resolves on the day of the build.
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
RUN uv export --frozen --no-dev --extra gcs --no-emit-project -o /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt
COPY src ./src
COPY dashboard ./dashboard
COPY docs/data ./docs/data
RUN pip install --no-cache-dir --no-deps .

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
# Cloud Run terminates TLS and forwards plain HTTP. Trust its X-Forwarded-*
# headers (it is the only way in), so request.url.scheme is "https" and the
# session cookie is set Secure.
CMD ["uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
