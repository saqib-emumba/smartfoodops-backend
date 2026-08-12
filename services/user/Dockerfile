FROM python:3.11-slim

WORKDIR /app

# Install service dependencies (bcrypt for password hashing, email-validator via pydantic[email])
RUN pip install --no-cache-dir \
    fastapi==0.115.6 \
    uvicorn==0.34.0 \
    "pydantic[email]==2.10.4" \
    psycopg2-binary==2.9.10 \
    bcrypt==4.2.1

# Copy application source code
COPY . /app

EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
