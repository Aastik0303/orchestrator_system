FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/.cache/huggingface

WORKDIR /app

# CPU-only torch first: the default Linux wheel pulls several GB of CUDA
# libraries that this service does not use.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# Bake the embedding model into the image so EMBEDDING_LOCAL_FILES_ONLY=true
# works without network access at runtime.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

COPY backend/app ./app
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./alembic.ini
COPY backend/evals ./evals

# Run as an unprivileged user; only the storage volume is writable.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p storage/uploads storage/outputs \
    && chown -R appuser:appuser /app/storage /app/.cache
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --retries=10 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
