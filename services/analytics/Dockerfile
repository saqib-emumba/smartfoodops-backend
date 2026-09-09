FROM python:3.11-slim

WORKDIR /app

# Requirements copied before source so editing a .py file does not invalidate the pip
# layer — same convention every service's Dockerfile follows.
COPY common/requirements.txt /app/common/requirements.txt
COPY analytics/requirements.txt /app/analytics/requirements.txt
RUN pip install --no-cache-dir -r /app/analytics/requirements.txt

COPY common/ /app/common/
COPY analytics/ /app/analytics/

EXPOSE 8008

CMD ["uvicorn", "analytics.main:app", "--host", "0.0.0.0", "--port", "8008"]
