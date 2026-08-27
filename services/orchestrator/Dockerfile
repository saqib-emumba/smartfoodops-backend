FROM python:3.11-slim

WORKDIR /app

# Requirements are copied before the source so editing a .py file does not invalidate
# the pip layer. The service file `-r`s the shared chassis list, and the two land in the
# same relative positions they occupy in the source tree so that reference resolves.
COPY common/requirements.txt /app/common/requirements.txt
COPY orchestrator/requirements.txt /app/orchestrator/requirements.txt
RUN pip install --no-cache-dir -r /app/orchestrator/requirements.txt

# Build context is ./services so the shared chassis can be copied alongside this
# service, and the service is copied as a package so its own modules import absolutely.
COPY common/ /app/common/
COPY orchestrator/ /app/orchestrator/

EXPOSE 8007

CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8007"]
