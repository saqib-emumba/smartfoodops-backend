FROM python:3.11-slim

WORKDIR /app

# Pins live in requirements.txt rather than here: the repo had no dependency manifest
# at all, so nothing could feed a vulnerability scan or answer "what is in this image".
# (httpx for the User Service account check and recording a rider's report against the
#  order, PyJWT[crypto] to verify RS256 access tokens — this service never signs any,
#  temporalio to signal the order's saga directly since D47 — a client, never a worker.)
#
# Requirements are copied before the source so editing a .py file does not invalidate
# the pip layer. The service file `-r`s the shared chassis list, and the two land in the
# same relative positions they occupy in the source tree so that reference resolves.
COPY common/requirements.txt /app/common/requirements.txt
COPY rider/requirements.txt /app/rider/requirements.txt
RUN pip install --no-cache-dir -r /app/rider/requirements.txt

# Build context is ./services so the shared chassis can be copied alongside this
# service, and the service is copied as a package so its own modules import absolutely.
COPY common/ /app/common/
COPY rider/ /app/rider/

EXPOSE 8006

CMD ["uvicorn", "rider.main:app", "--host", "0.0.0.0", "--port", "8006"]
