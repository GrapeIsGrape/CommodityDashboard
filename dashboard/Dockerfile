# Build context is the repo root so common/ and config/ are available.
FROM python:3.12-slim

WORKDIR /app

COPY dashboard/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY config/ ./config/
COPY dashboard/ ./dashboard/

EXPOSE 8000

CMD ["uvicorn", "dashboard.main:app", "--host", "0.0.0.0", "--port", "8000"]
