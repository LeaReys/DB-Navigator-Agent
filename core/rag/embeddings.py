"""
Единая точка построения эмбеддинг-функции для RAG.

Зачем модуль:
  - Модель строится единожды на процесс, индексатор 
  и ретривер переиспользуют один объект.
"""

from __future__ import annotations

import logging
import os

from core.config import settings

logger = logging.getLogger(__name__)

# Модель эмбеддингов - локальная, мультиязычная (рус/eng), ~120 MB.
EMBEDDING_MODEL = "intfloat/multilingual-e5-small"

# Имя коллекции ChromaDB - общее для индексатора и ретривера.
COLLECTION_NAME = "db_schema"

# Единственный экземпляр эмбеддинг-функции на весь процесс.
_embedding_function = None


def _apply_hf_env() -> None:
    """
    Прокидывает HF_TOKEN из настроек в стандартные переменные окружения
    huggingface_hub, если он задан и ещё не выставлен снаружи.
    """
    token = (settings.hf_token or "").strip()
    if token:
        os.environ.setdefault("HF_TOKEN", token)
        os.environ.setdefault("HUGGING_FACE_HUB_TOKEN", token)
        os.environ.setdefault("HUGGINGFACE_HUB_TOKEN", token)


def get_embedding_function():
    """
    Возвращает единый SentenceTransformerEmbeddingFunction.
    """
    global _embedding_function
    if _embedding_function is None:
        _apply_hf_env()

        # Импорт внутри функции: тяжёлые зависимости тянем только когда
        # реально строим эмбеддинги (а не при импорте модуля).
        from chromadb.utils.embedding_functions import (
            SentenceTransformerEmbeddingFunction,
        )

        offline = os.environ.get("HF_HUB_OFFLINE", "0") == "1"
        logger.info(
            "Загрузка эмбеддинг-модели %s (offline=%s)…", EMBEDDING_MODEL, offline
        )
        _embedding_function = SentenceTransformerEmbeddingFunction(
            model_name=EMBEDDING_MODEL,
        )
        logger.info("Эмбеддинг-модель загружена (один раз на процесс).")

    return _embedding_function
