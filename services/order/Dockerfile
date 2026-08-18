FROM python:3.11-slim

WORKDIR /app

# Install service dependencies (httpx for Menu Service menu lookups and audit logging,
# PyJWT[crypto] to verify RS256 access tokens — this service never signs any)
RUN pip install --no-cache-dir \
    fastapi==0.115.6 \
    uvicorn==0.34.0 \
    pydantic==2.10.4 \
    psycopg2-binary==2.9.10 \
    httpx==0.28.1 \
    "PyJWT[crypto]==2.10.1"

# Build context is ./services so the shared chassis can be copied alongside this service.
COPY common/ /app/common/
COPY order/ /app/

EXPOSE 8004

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8004"]
