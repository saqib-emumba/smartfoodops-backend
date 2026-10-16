"""Conversation memory for the assistant, in Redis (Week 5, D63).

Keyed `ai:conv:{user_id}:{conversation_id}`: the user id is part of the key, so a conversation id
is a label within one user's history and never a way into someone else's. A capped list with a TTL,
so memory is bounded in both length and time.

Every method degrades: Redis down means an empty history (the question is still answered, just
without follow-up context) and a skipped save, logged, never raised — the same stance the menu
cache takes. Blocking client, so callers on the event loop use `asyncio.to_thread`.
"""

import json
import re
from logging import Logger

import redis

from common.redis_store import RedisStore
from ai import config

CONVERSATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _key(user_id: str, conversation_id: str) -> str:
    return f"ai:conv:{user_id}:{conversation_id}"


class ConversationStore(RedisStore):
    def __init__(self, url: str, *, logger: Logger):
        super().__init__(url, logger=logger, name="Conversation store")

    def load(self, user_id: str, conversation_id: str) -> list[dict]:
        """Prior messages, oldest first, as `{"role": "user"|"assistant", "content": str}`."""
        try:
            raw = self._client.lrange(_key(user_id, conversation_id), 0, -1)
        except redis.RedisError as exc:
            self._logger.warning("Could not load conversation history: %s", exc)
            return []
        return [json.loads(item) for item in raw]

    def append(self, user_id: str, conversation_id: str, question: str, answer: str) -> None:
        """Record one exchange, trimming to the newest `CONVERSATION_MAX_MESSAGES` messages."""
        key = _key(user_id, conversation_id)
        try:
            pipe = self._client.pipeline()
            pipe.rpush(
                key,
                json.dumps({"role": "user", "content": question}),
                json.dumps({"role": "assistant", "content": answer}),
            )
            pipe.ltrim(key, -config.CONVERSATION_MAX_MESSAGES, -1)
            pipe.expire(key, config.CONVERSATION_TTL_SECONDS)
            pipe.execute()
        except redis.RedisError as exc:
            self._logger.warning("Could not save conversation history: %s", exc)
