"""
Поиск по проиндексированной схеме БД.

Принимает текстовый запрос пользователя, ищет похожие документы
в ChromaDB и возвращает список MetadataChunk.

Важно: retriever — read-only. Он только читает из индекса, не изменяет его.
"""

from __future__ import annotations

import logging

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

from config import settings
from schemes.models import MetadataChunk

logger = logging.getLogger(__name__)

COLLECTION_NAME  = "db_schema"
EMBEDDING_MODEL  = "intfloat/multilingual-e5-small"
MIN_SIMILARITY = 0.3        # Минимальный score для включения результата в ответ.


class SchemaRetriever:
    """
    Выполняет семантический поиск по индексу схемы БД.
    """

    def __init__(self) -> None:
        self._collection = self._load_collection()

    def _load_collection(self) -> chromadb.Collection:
        """Открывает существующую коллекцию ChromaDB."""
        ef = SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL)

        client = chromadb.PersistentClient(path=settings.chroma_persist_dir)

        try:
            collection = client.get_collection(
                name=COLLECTION_NAME,
                embedding_function=ef,
            )
            logger.info(
                f"Индекс загружен: {collection.count()} документов "
                f"из '{settings.chroma_persist_dir}'"
            )
            return collection
        except Exception as e:
            raise RuntimeError(
                f"Индекс не найден в '{settings.chroma_persist_dir}'. "
                f"Необходимо запустить сначала: python -m rag.indexer\n"
                f"Текст ошибки: {e}"
            ) from e

    def search(
        self,
        query:  str,
        top_k:  int | None = None,
    ) -> list[MetadataChunk]:
        """
        Ищет релевантные таблицы по запросу.

        Args:
            query: вопрос пользователя
            top_k: количество результатов (по умолчанию из settings.rag_top_k)

        Returns:
            Список MetadataChunk, отсортированный по убыванию релевантности.
            Если индекс пустой или ничего не найдено — пустой список.
        """
        limit = top_k if top_k is not None else settings.rag_top_k

        if self._collection.count() == 0:
            logger.warning("Индекс пустой. Запусти python -m rag.indexer")
            return []

        logger.debug(f"Поиск: '{query}', top_k={limit}")

        # добавляем префикс "query: " перед запросом.
        # Без префикса запрос эмбеддится в другом «пространстве» чем документы,
        # и косинусное сходство между ними ниже, чем должно быть.
        results = self._collection.query(
            query_texts = [f"query: {query}"],          # ← префикс e5
            n_results   = min(limit, self._collection.count()),
            include     = ["documents", "metadatas", "distances"],
        )

        chunks = self._parse_results(results, query)

        # Фильтруем по минимальному score
        filtered = [c for c in chunks if c.score >= MIN_SIMILARITY]

        logger.info(
            f"Поиск '{query}': найдено {len(chunks)}, "
            f"после фильтрации (score>={MIN_SIMILARITY}): {len(filtered)}"
        )

        return filtered

    def _parse_results(
        self,
        results: dict,
        query:   str,
    ) -> list[MetadataChunk]:
        """
        Преобразует сырой ответ ChromaDB в список MetadataChunk.

        ChromaDB возвращает distance (чем меньше — тем лучше).
        Переводим в similarity (чем больше — тем лучше) для удобства.
        """
        chunks: list[MetadataChunk] = []

        # ChromaDB возвращает вложенные списки (один запрос → один элемент)
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        for meta, distance in zip(metadatas, distances):
            # Cosine distance → similarity
            # ChromaDB: distance ∈ [0, 2], где 0 = идентично
            similarity = round(1.0 - distance / 2.0, 3)
            similarity = max(0.0, min(1.0, similarity))  # clip в [0, 1]

            # Колонки хранятся как строка "col1, col2, col3"
            columns_raw = meta.get("columns", "")
            columns = [c.strip() for c in columns_raw.split(",") if c.strip()]

            chunk = MetadataChunk(
                table_name  = meta["table_name"],
                server      = meta["server"],
                database    = meta["database"],
                description = meta.get("description") or "",
                score       = similarity,
                columns     = columns,
            )
            chunks.append(chunk)

        return chunks

    def is_ready(self) -> bool:
        """Проверяет, что индекс существует и не пустой."""
        try:
            return self._collection.count() > 0
        except Exception:
            return False


# =============================================================
# Singleton: один экземпляр на всё приложение
# =============================================================

# Ленивая инициализация — создаём только при первом обращении,
# чтобы не падать при старте если индекс ещё не построен.
_retriever_instance: SchemaRetriever | None = None


def get_retriever() -> SchemaRetriever:
    """Возвращает единственный экземпляр SchemaRetriever."""
    global _retriever_instance
    if _retriever_instance is None:
        _retriever_instance = SchemaRetriever()
    return _retriever_instance