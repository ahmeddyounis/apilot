"""Embedding providers for near-duplicate detection.

Default is local fastembed (ONNX, no API key). The deterministic FakeEmbeddings —
hashed bag-of-tokens, normalized — keeps tests and CI offline while preserving the
property that similar texts land close in cosine space.
"""

import hashlib
import math
import re
from functools import lru_cache
from typing import Protocol

import structlog

from app.core.config import Settings

logger = structlog.get_logger(__name__)

EMBEDDING_DIM = 384

_TOKEN_PATTERN = re.compile(r"[\w.]+")


class EmbeddingProvider(Protocol):
    model_name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbeddings:
    """Deterministic hashed bag-of-tokens embedding (no ML, no downloads)."""

    model_name = "fake-hash-embeddings"
    dim = EMBEDDING_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * self.dim
            for token in _TOKEN_PATTERN.findall(text.casefold()):
                digest = hashlib.sha256(token.encode()).digest()
                index = int.from_bytes(digest[:4], "big") % self.dim
                sign = 1.0 if digest[4] % 2 == 0 else -1.0
                vector[index] += sign
            norm = math.sqrt(sum(component * component for component in vector)) or 1.0
            vectors.append([component / norm for component in vector])
        return vectors


class FastEmbedProvider:
    """Local ONNX embeddings via fastembed (installed with the `eval` group)."""

    def __init__(self, model_name: str) -> None:
        from fastembed import TextEmbedding  # deferred: heavy optional dependency

        self.model_name = model_name
        self.dim = EMBEDDING_DIM
        self._model = TextEmbedding(model_name=model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.embed(texts)]


@lru_cache(maxsize=4)
def _cached_provider(mode: str, model_name: str) -> EmbeddingProvider:
    """One provider per (mode, model), not one per invoice.

    FastEmbedProvider.__init__ loads and initialises an ONNX model from disk.
    build_embedding_provider was called inside the per-invoice pipeline, so every
    single invoice paid that cost — synchronously, on the worker's event loop, which
    also stalled every other job in flight. Keyed rather than a bare global so a test
    that flips the mode still gets the right provider.
    """
    if mode == "fake":
        return FakeEmbeddings()
    try:
        return FastEmbedProvider(model_name)
    except ImportError:
        # Loud, because the failure is invisible otherwise: the pipeline keeps
        # running and near-duplicate detection keeps reporting scores, but they come
        # from hashed bag-of-tokens vectors that cannot recognise a resubmission
        # that has been reworded. The runtime image installs the `embeddings` group
        # precisely so this path is not taken in a real deployment.
        logger.error(
            "fastembed_unavailable_near_duplicate_detection_degraded",
            hint="install the 'embeddings' dependency group",
        )
        return FakeEmbeddings()


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    return _cached_provider(settings.resolved_llm_mode, settings.embedding_model)
