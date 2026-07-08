# bge-m3 embedding sidecar (P3 RAG embed tier). CPU-only by default.
#
# Serves POST /embed {texts:[...]} -> {embeddings:[[...1024...]]} on :8090 using
# BAAI/bge-m3 via sentence-transformers. The sub2api LLM relay has no embeddings
# endpoint, so the SmartChat API (EMBED_BASE_URL=http://embed:8090) routes the
# embed tier here. bge-m3 on CPU comfortably serves KB ingest + query volumes;
# for GPU, switch to a CUDA base image and set EMBED_DEVICE=cuda.
#
# The model (~2.3GB) is pre-downloaded at build time so the container is
# offline-ready and has no cold-start download on first request.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models \
    SENTENCE_TRANSFORMERS_HOME=/models \
    EMBED_MODEL=BAAI/bge-m3 \
    EMBED_DEVICE=cpu

WORKDIR /srv/embed

# CPU-only torch wheel keeps the image lean (no CUDA runtime).
# torch>=2.6 required: newer transformers block torch.load on <2.6 for
# CVE-2025-32434, which bge-m3's checkpoint load hits at build time.
RUN pip install \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        "torch==2.6.0" \
        "sentence-transformers==3.1.1" \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.30" \
        "pydantic>=2.8"

COPY infra/embed_server.py ./embed_server.py

# Warm the model cache into the image layer.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3', device='cpu')"

EXPOSE 8090
CMD ["uvicorn", "embed_server:app", "--host", "0.0.0.0", "--port", "8090"]
