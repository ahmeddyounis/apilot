"""Embedding providers for near-duplicate detection.

Default is local fastembed (ONNX, no API key). The deterministic FakeEmbeddings —
hashed bag-of-tokens, normalized — keeps tests and CI offline while preserving the
property that similar texts land close in cosine space.
"""

import hashlib
import math
import re
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


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    if settings.resolved_llm_mode == "fake":
        return FakeEmbeddings()
    try:
        return FastEmbedProvider(settings.embedding_model)
    except ImportError:
        logger.warning(
            "fastembed_unavailable",
            hint="install the 'eval' dependency group for real near-dup embeddings",
        )
        return FakeEmbeddings()
