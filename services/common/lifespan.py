"""Compose several dependency lifespans into the single one FastAPI accepts.

Three services solved this three ways: most pass `db.lifespan` straight in, the Menu Service
hand-nests its cache inside it, and the Order Service uses an `AsyncExitStack`. This is that
last answer made the only answer, so shutdown ordering is not re-decided per service.
"""

from contextlib import AsyncExitStack, asynccontextmanager


def compose_lifespan(*lifespans):
    """Enter each lifespan in order and exit in reverse. Pass to `FastAPI(lifespan=...)`."""

    @asynccontextmanager
    async def composed(app):
        async with AsyncExitStack() as stack:
            for lifespan in lifespans:
                await stack.enter_async_context(lifespan(app))
            yield

    return composed
