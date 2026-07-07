# One image for all three entrypoints:
#   dashboard (default):  uvicorn dashboard.app:app
#   ingest job:           python -m alex311.ingest incremental
#   health job:           python -m alex311.health
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY dashboard ./dashboard
RUN pip install --no-cache-dir ".[gcs]"

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8080"]
