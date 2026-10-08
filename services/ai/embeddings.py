"""Turning text into vectors, behind one small interface (Week 4, D59).

Everything that embeds — the ingestion worker at write time, the search API at query time —
goes through `Embedder`, so the model is chosen in exactly one place (`build_embedder`) and a
document and the query that should match it can never be embedded by different models. That
is not a nicety: vectors from two models are not comparable, and a mismatch does not fail,
it just returns nonsense.
"""

from typing import Protocol

import httpx
import numpy as np

from common.config import required
from ai import config


class Embedder(Protocol):
    """Anything that can embed a batch of texts into `config.EMBEDDING_DIMENSIONS` floats."""

    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class LocalEmbedder:
    """all-MiniLM-L6-v2 on onnxruntime (fastembed), running inside this container.

    The model is baked into the image at build time (see the Dockerfile), so constructing this
    reads it from disk rather than fetching anything. `embed` is CPU-bound and synchronous —
    callers on the event loop should run it in a thread.
    """

    def __init__(self, model_name: str = config.LOCAL_EMBEDDING_MODEL):
        # Imported here, not at module level: fastembed pulls in onnxruntime, which the
        # OpenAI provider and the unit-level tests have no use for.
        from fastembed import TextEmbedding

        self.name = model_name
        self._model = TextEmbedding(model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.embed(texts)]


class OpenAIEmbedder:
    """OpenAI's embeddings API, asked for vectors of the width the schema stores.

    `text-embedding-3-*` models accept a `dimensions` parameter that returns a shortened
    vector, which is what lets this provider share the `vector(384)` columns with the local
    one. Needs `OPENAI_API_KEY`, which is why it is read here and nowhere else.
    """

    def __init__(self, model_name: str = config.OPENAI_EMBEDDING_MODEL):
        self.name = model_name
        self._key = required("OPENAI_API_KEY")

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = httpx.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {self._key}"},
            json={
                "model": self.name,
                "input": texts,
                "dimensions": config.EMBEDDING_DIMENSIONS,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        rows = sorted(response.json()["data"], key=lambda row: row["index"])
        return [row["embedding"] for row in rows]


def build_embedder() -> Embedder:
    """The provider `EMBEDDING_PROVIDER` names. Unknown values fail loudly at startup."""
    if config.EMBEDDING_PROVIDER == "local":
        return LocalEmbedder()
    if config.EMBEDDING_PROVIDER == "openai":
        return OpenAIEmbedder()
    raise RuntimeError(
        f"EMBEDDING_PROVIDER={config.EMBEDDING_PROVIDER!r} is not supported; use 'local' or 'openai'."
    )


def as_vector(values: list[float]) -> np.ndarray:
    """An embedding as the float32 array pgvector's adapter turns into a `vector` parameter.

    The `Embedder` interface stays plain lists — a provider should not have to know what the
    database driver wants — and conversion happens once, at the point a value is handed to SQL.
    float32 because that is what the `vector` type stores; sending float64 only to have the
    server narrow it would double the bytes on the wire for nothing.
    """
    return np.asarray(values, dtype=np.float32)
