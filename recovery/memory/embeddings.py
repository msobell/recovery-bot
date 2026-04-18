from __future__ import annotations

import logging
from typing import List

logger = logging.getLogger(__name__)
_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading all-MiniLM-L6-v2...")
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def get_embedding(text: str) -> List[float]:
    return _get_model().encode(text).tolist()


def get_embeddings(texts: List[str]) -> List[List[float]]:
    return _get_model().encode(texts).tolist()
