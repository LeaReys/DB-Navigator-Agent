"""
Единая точка построения эмбеддинг-функции для RAG.

Зачем модуль:
  - Здесь модель строится РОВНО ОДИН РАЗ на процесс (модульный синглтон),
    и обе стороны (индексатор и ретривер) переиспользуют один объект.
"""

from __future__ import annotations

import logging
import os

from core.config import settings

logger = logging.getLogger(__name__)

# Модель эмбеддингов — локальная, мультиязычная (рус/eng), ~120 MB.
EMBEDDING_MODEL = "intfloat/multilingual-e5-small"

# Имя коллекции ChromaDB — общее для индексатора и ретривера.
COLLECTION_NAME = "db_schema"

# Единственный экземпляр эмбеддинг-функции на весь процесс.
_embedding_function = None


def _apply_hf_env() -> None:
    """
    Прокидывает HF_TOKEN из настроек в стандартные переменные окружения
    huggingface_hub, если он задан и ещё не выставлен снаружи.
    Оффлайн-режим НЕ навязываем кодом — он управляется переменной
    HF_HUB_OFFLINE (её ставит docker-compose).
    """
    token = (settings.hf_token or "").strip()
    if token:
        os.environ.setdefault("HF_TOKEN", token)
        os.environ.setdefault("HUGGING_FACE_HUB_TOKEN", token)
        os.environ.setdefault("HUGGINGFACE_HUB_TOKEN", token)


def get_embedding_function():
    """
    Возвращает единый SentenceTransformerEmbeddingFunction.

    Модель загружается только при ПЕРВОМ вызове, далее отдаётся готовый объект.
    Оба модуля RAG (indexer, retriever) обязаны звать именно эту функцию,
    чтобы не плодить копии модели в памяти.
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
