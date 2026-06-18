"""
Поиск по проиндексированной схеме БД.

Принимает текстовый запрос пользователя, ищет похожие документы
в ChromaDB и возвращает список MetadataChunk.

Важно: retriever — read-only. Он только читает из индекса, не изменяет его.

Загрузка модели эмбеддингов вынесена в core.rag.embeddings и происходит
один раз на процесс — retriever и indexer переиспользуют общий объект.
"""

from __future__ import annotations

import logging

import chromadb

from core.config import settings
from core.rag.embeddings import COLLECTION_NAME, get_embedding_function
from core.schemas.models import MetadataChunk

logger = logging.getLogger(__name__)

MIN_SIMILARITY = 0.3        # Минимальный score для включения результата в ответ.


class SchemaRetriever:
    """
    Выполняет семантический поиск по индексу схемы БД.

    Конструктор НИКОГДА не падает, даже если индекс ещё не построен:
    в этом случае создаётся пустая коллекция, is_ready() вернёт False,
    а вызывающий код (search_metadata) уйдёт на SQL-fallback.
    Это снимает прежний баг, когда падение в __init__ заставляло
    get_retriever() пересоздавать объект и заново грузить модель в цикле.
    """

    def __init__(self) -> None:
        self._client = chromadb.PersistentClient(path=settings.chroma_persist_dir)
        # get_or_create — не бросает исключение, если коллекции ещё нет.
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=get_embedding_function(),
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            "Индекс открыт: %s документов из '%s'",
            self._collection.count(), settings.chroma_persist_dir,
        )

    def search(
        self,
        query:  str,
        top_k:  int | None = None,
    ) -> list[MetadataChunk]:
        """
        Ищет релевантные таблицы по запросу.

        Returns:
            Список MetadataChunk, отсортированный по убыванию релевантности.
            Если индекс пустой или ничего не найдено — пустой список.
        """
        limit = top_k if top_k is not None else settings.rag_top_k

        count = self._collection.count()
        if count == 0:
            logger.warning("Индекс пустой. Запусти: python -m core.rag.indexer")
            return []

        logger.debug("Поиск: '%s', top_k=%s", query, limit)

        # Префикс "query: " обязателен для модели e5 — без него запрос
        # эмбеддится в другом «пространстве», чем документы (passage: ...),
        # и косинусное сходство занижается.
        results = self._collection.query(
            query_texts=[f"query: {query}"],
            n_results=min(limit, count),
            include=["documents", "metadatas", "distances"],
        )

        chunks = self._parse_results(results, query)
        filtered = [c for c in chunks if c.score >= MIN_SIMILARITY]

        logger.info(
            "Поиск '%s': найдено %s, после фильтра (score>=%s): %s",
            query, len(chunks), MIN_SIMILARITY, len(filtered),
        )
        return filtered

    def _parse_results(
        self,
        results: dict,
        query:   str,
    ) -> list[MetadataChunk]:
        """
        Преобразует сырой ответ ChromaDB в список MetadataChunk.
        ChromaDB возвращает distance (меньше — лучше), переводим в similarity.
        """
        chunks: list[MetadataChunk] = []

        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        for meta, distance in zip(metadatas, distances):
            similarity = round(1.0 - distance / 2.0, 3)
            similarity = max(0.0, min(1.0, similarity))

            columns_raw = meta.get("columns", "")
            columns = [c.strip() for c in columns_raw.split(",") if c.strip()]

            chunks.append(MetadataChunk(
                table_name  = meta["table_name"],
                server      = meta["server"],
                database    = meta["database"],
                description = meta.get("description") or "",
                score       = similarity,
                columns     = columns,
            ))

        return chunks

    def count(self) -> int:
        """Число документов в индексе (дёшево, без загрузки модели)."""
        try:
            return self._collection.count()
        except Exception:
            return 0

    def is_ready(self) -> bool:
        """True, если индекс существует и не пустой."""
        return self.count() > 0


# =============================================================
# Singleton: один экземпляр на всё приложение
# =============================================================

_retriever_instance: SchemaRetriever | None = None


def get_retriever() -> SchemaRetriever:
    """Возвращает единственный экземпляр SchemaRetriever (ленивая инициализация)."""
    global _retriever_instance
    if _retriever_instance is None:
        _retriever_instance = SchemaRetriever()
    return _retriever_instance
