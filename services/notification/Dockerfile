FROM python:3.11-slim

WORKDIR /app

COPY common/requirements.txt /app/common/requirements.txt
COPY notification/requirements.txt /app/notification/requirements.txt
RUN pip install --no-cache-dir -r /app/notification/requirements.txt

COPY common/ /app/common/
COPY notification/ /app/notification/

# No EXPOSE/CMD default: docker-compose.yml gives `notification-consumer` and
# `notification-worker` their own `command:` from this one image (Week 3, D45).
