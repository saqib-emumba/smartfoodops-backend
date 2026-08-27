FROM python:3.11-slim

WORKDIR /app

# Pins live in requirements.txt rather than here: the repo had no dependency manifest
# at all, so nothing could feed a vulnerability scan or answer "what is in this image".
# (bcrypt for password hashing, email-validator via pydantic[email], PyJWT[crypto] to
#  sign RS256 access tokens (the only service that signs any), redis for refresh sessions.)
#
# Requirements are copied before the source so editing a .py file does not invalidate
# the pip layer. The service file `-r`s the shared chassis list, and the two land in the
# same relative positions they occupy in the source tree so that reference resolves.
COPY common/requirements.txt /app/common/requirements.txt
COPY user/requirements.txt /app/user/requirements.txt
RUN pip install --no-cache-dir -r /app/user/requirements.txt

# Build context is ./services so the shared chassis can be copied alongside this
# service, and the service is copied as a package so its own modules import absolutely.
COPY common/ /app/common/
COPY user/ /app/user/

EXPOSE 8001

CMD ["uvicorn", "user.main:app", "--host", "0.0.0.0", "--port", "8001"]
