FROM python:3.11-slim

WORKDIR /app

# Pins live in requirements.txt rather than here: the repo had no dependency manifest
# at all, so nothing could feed a vulnerability scan or answer "what is in this image".
# (redis for the cache-aside layer in front of the `menus` table, httpx for the
#  Restaurant Service lookup, PyJWT[crypto] to verify RS256 tokens — never to sign them.)
#
# Requirements are copied before the source so editing a .py file does not invalidate
# the pip layer. The service file `-r`s the shared chassis list, and the two land in the
# same relative positions they occupy in the source tree so that reference resolves.
COPY common/requirements.txt /app/common/requirements.txt
COPY menu/requirements.txt /app/menu/requirements.txt
RUN pip install --no-cache-dir -r /app/menu/requirements.txt

# Build context is ./services so the shared chassis can be copied alongside this
# service, and the service is copied as a package so its own modules import absolutely.
COPY common/ /app/common/
COPY menu/ /app/menu/

EXPOSE 8003

CMD ["uvicorn", "menu.main:app", "--host", "0.0.0.0", "--port", "8003"]
