FROM python:3.11-slim

WORKDIR /app

# Pins live in requirements.txt rather than here: the repo had no dependency manifest
# at all, so nothing could feed a vulnerability scan or answer "what is in this image".
# (httpx for the request-path lookups and the saga hand-off; PyJWT[crypto] to verify RS256
#  tokens, never to sign them. No temporalio — see requirements.txt; D36 moved the worker,
#  and this image's Temporal client, to services/orchestrator/.)
#
# Requirements are copied before the source so editing a .py file does not invalidate
# the pip layer. The service file `-r`s the shared chassis list, and the two land in the
# same relative positions they occupy in the source tree so that reference resolves.
COPY common/requirements.txt /app/common/requirements.txt
COPY order/requirements.txt /app/order/requirements.txt
RUN pip install --no-cache-dir -r /app/order/requirements.txt

# Build context is ./services so the shared chassis can be copied alongside this
# service, and the service is copied as a package so its own modules import absolutely.
COPY common/ /app/common/
COPY order/ /app/order/

EXPOSE 8004

CMD ["uvicorn", "order.main:app", "--host", "0.0.0.0", "--port", "8004"]
