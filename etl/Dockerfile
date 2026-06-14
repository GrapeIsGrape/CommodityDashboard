# Build context is the repo root so common/, config/, and migrations/ are available.
FROM python:3.12-slim

WORKDIR /app

COPY etl/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY config/ ./config/
COPY migrations/ ./migrations/
COPY etl/ ./etl/

CMD ["python", "-m", "etl.run"]
