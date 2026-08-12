FROM python:3.11-slim

WORKDIR /app

# Install service dependencies (motor for async MongoDB, redis for cache health, httpx for inter-service calls)
RUN pip install --no-cache-dir \
    fastapi==0.115.6 \
    uvicorn==0.34.0 \
    pydantic==2.10.4 \
    motor==3.6.0 \
    redis==5.2.1 \
    httpx==0.28.1

# Build context is ./services so the shared chassis can be copied alongside this service.
COPY common/ /app/common/
COPY menu/ /app/

EXPOSE 8003

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003"]
