FROM python:3.11-slim

WORKDIR /app

# Install service dependencies (httpx for inter-service calls to the User Service)
RUN pip install --no-cache-dir \
    fastapi==0.115.6 \
    uvicorn==0.34.0 \
    pydantic==2.10.4 \
    psycopg2-binary==2.9.10 \
    httpx==0.28.1

# Copy application source code
COPY . /app

EXPOSE 8002

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
