FROM python:3.11-slim

WORKDIR /app

# Requirements copied before source so editing a .py file does not invalidate the pip
# layer — same convention every service's Dockerfile follows.
COPY common/requirements.txt /app/common/requirements.txt
COPY ai/requirements.txt /app/ai/requirements.txt
RUN pip install --no-cache-dir -r /app/ai/requirements.txt

# The embedding model is downloaded now, at build time, into the image — not on first request.
# A container that fetched it at startup would need outbound network access to start, would be
# slow to become healthy, and would behave differently on a machine without it. The cache path
# is fixed so the runtime finds exactly what this layer wrote.
ENV FASTEMBED_CACHE_PATH=/app/.model-cache
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2')"

COPY common/ /app/common/
COPY ai/ /app/ai/

EXPOSE 8009

CMD ["uvicorn", "ai.main:app", "--host", "0.0.0.0", "--port", "8009"]
