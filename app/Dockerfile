# Pin the base image. Candidates: pin YOUR built image too (tag or digest), never :latest.
FROM python:3.12.4-slim-bookworm

RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

# Install deps first so this layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

USER appuser
EXPOSE 8080
# gunicorn (not the Flask dev server); 2 workers is plenty for this tiny API.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "main:app"]
