"""The chat model, chosen in one place (Week 5, D63).

The generation side of what `embeddings.py` does for retrieval: everything that talks to a
language model asks `build_chat_model` for it, so the provider is a deploy-time setting rather
than something a graph node knows about. Both providers come back as a LangChain
`BaseChatModel`, which is the type LangGraph nodes consume and which streams token chunks
through `astream`.

The embedding model and the chat model are chosen independently on purpose (D63): vectors
from two embedders are not comparable, so that choice is pinned to the schema; a chat model
carries no such state and can be swapped freely.
"""

from langchain_core.language_models.chat_models import BaseChatModel

from common.config import required
from ai import config


def build_chat_model(*, temperature: float | None = None) -> BaseChatModel:
    """The provider `LLM_PROVIDER` names. Unknown values fail loudly at startup.

    `temperature` lets a caller override the service default for one model instance (the
    partner copywriting tools could want more variety than the grounded assistant). Honoured by Ollama only:
    the Anthropic model in use rejects a non-default temperature.
    """
    temperature = config.LLM_TEMPERATURE if temperature is None else temperature

    if config.LLM_PROVIDER == "anthropic":
        # Imported here, not at module level: only the selected provider's package is loaded.
        from langchain_anthropic import ChatAnthropic

        # No `temperature`: claude-sonnet-5-5 rejects a non-default value ("`temperature` is not supported for
        # claude-sonnet-5-5 at non-default values"), which showed up as a 503 on the very first real call. The
        # grounding does not rest on a low temperature anyway (the guardrail checks the output), so the model's
        # own default is used. `temperature` still applies to Ollama below.
        return ChatAnthropic(
            model=config.ANTHROPIC_MODEL,
            api_key=required("ANTHROPIC_API_KEY"),
            max_tokens=config.LLM_MAX_TOKENS,
            timeout=config.LLM_TIMEOUT_SECONDS,
        )
    if config.LLM_PROVIDER == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=config.OLLAMA_MODEL,
            base_url=config.OLLAMA_BASE_URL,
            temperature=temperature,
            num_predict=config.LLM_MAX_TOKENS,
        )
    raise RuntimeError(
        f"LLM_PROVIDER={config.LLM_PROVIDER!r} is not supported; use 'anthropic' or 'ollama'."
    )


def model_name() -> str:
    """The model id for the selected provider, for spans and logs without building a client."""
    if config.LLM_PROVIDER == "anthropic":
        return config.ANTHROPIC_MODEL
    return config.OLLAMA_MODEL if config.LLM_PROVIDER == "ollama" else config.LLM_PROVIDER


def classify_failure(exc: Exception) -> str:
    """A provider error as a metric label, without importing any provider's SDK.

    Matched on the exception's class name (and `status_code`), because `anthropic`, `ollama` and
    `httpx` each raise their own types and importing all of them here would defeat loading only
    the selected provider.
    """
    name = type(exc).__name__.lower()
    status = getattr(exc, "status_code", None)
    if "timeout" in name:
        return "timeout"
    if "ratelimit" in name or status == 429:
        return "rate_limit"
    if "authentication" in name or "permission" in name or status in (401, 403):
        return "auth"
    if "connection" in name or "overloaded" in name or "internalserver" in name or (status or 0) >= 500:
        return "unavailable"
    return "other"


async def call_model(model: BaseChatModel, messages: list, *, node: str) -> str:
    """One chat-model call, metered, with provider failures turned into a plain `503`.

    The customer is told the assistant is unavailable and nothing else: a provider's error text
    can carry request ids, quota details or a key's last characters, none of which belongs in a
    response. The detail goes to the log; the kind goes to `sfo_ai_llm_failures_total`.
    """
    import logging

    from opentelemetry import trace

    from common.errors import service_unavailable
    from ai.metrics import LLM_CALLS_TOTAL, LLM_FAILURES_TOTAL

    with trace.get_tracer("ai.llm").start_as_current_span(f"ai.llm.{node}") as span:
        span.set_attribute("ai.llm.model", model_name())
        try:
            reply = await model.ainvoke(messages)
        except Exception as exc:  # noqa: BLE001 - every provider failure maps to the same 503
            kind = classify_failure(exc)
            LLM_CALLS_TOTAL.labels(node=node, outcome="error").inc()
            LLM_FAILURES_TOTAL.labels(kind=kind).inc()
            span.set_attribute("ai.llm.failure", kind)
            logging.getLogger(config.PROCESS_NAME).error("Chat model failed in %s (%s): %s", node, kind, exc)
            raise service_unavailable("The assistant is temporarily unavailable; please try again") from exc
        LLM_CALLS_TOTAL.labels(node=node, outcome="ok").inc()
    return text_of(reply)


async def stream_model(model: BaseChatModel, messages: list, *, node: str):
    """`call_model`'s streaming sibling: yields text fragments as the model produces them.

    Same metering and the same failure contract. A failure part-way through a stream raises after
    some tokens have already been sent; the caller (the SSE endpoint) is what tells the client the
    stream ended badly.
    """
    import logging

    from opentelemetry import trace

    from common.errors import service_unavailable
    from ai.metrics import LLM_CALLS_TOTAL, LLM_FAILURES_TOTAL

    with trace.get_tracer("ai.llm").start_as_current_span(f"ai.llm.{node}") as span:
        span.set_attribute("ai.llm.model", model_name())
        try:
            async for chunk in model.astream(messages):
                fragment = text_of(chunk)
                if fragment:
                    yield fragment
        except Exception as exc:  # noqa: BLE001
            kind = classify_failure(exc)
            LLM_CALLS_TOTAL.labels(node=node, outcome="error").inc()
            LLM_FAILURES_TOTAL.labels(kind=kind).inc()
            span.set_attribute("ai.llm.failure", kind)
            logging.getLogger(config.PROCESS_NAME).error("Chat model failed in %s (%s): %s", node, kind, exc)
            raise service_unavailable("The assistant is temporarily unavailable; please try again") from exc
        LLM_CALLS_TOTAL.labels(node=node, outcome="ok").inc()


def text_of(message) -> str:
    """The text of a model message, whether `content` is a string or a list of content blocks."""
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "") if isinstance(block, dict) else str(block) for block in content
    )
