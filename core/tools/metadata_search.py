"""
Поиск таблиц и колонок по запросу.

Fallback: если индекс не построен (retriever не готов),
то автоматически возвращаемся к SQL LIKE поиску.
"""

from __future__ import annotations

import logging

from core.schemas.models import (
    MetadataChunk,
    MetadataSearchResult,
    ToolStatus,
)

logger = logging.getLogger(__name__)


def search_metadata(
    query: str,
    top_k: int | None = None,   # по умолчанию из settings.rag_top_k
) -> MetadataSearchResult:
    """
    Ищет релевантные таблицы и колонки по запросу пользователя.

    Использует ChromaDB.
    При недоступном индексе - fallback на SQL LIKE поиск.

    Returns:
        MetadataSearchResult со списком найденных таблиц
    """
    # Пробуем векторный поиск
    try:
        from core.rag.retriever import get_retriever
        retriever = get_retriever()

        if retriever.is_ready():
            return _vector_search(retriever, query, top_k)
        else:
            logger.warning("Индекс пустой - fallback на SQL поиск")
            return _sql_fallback_search(query, top_k)

    except RuntimeError as e:
        # Индекс не построен - используем SQL поиск
        logger.warning(f"RAG недоступен ({e}) - fallback на SQL поиск")
        return _sql_fallback_search(query, top_k)

    except Exception as e:
        logger.error(f"Неожиданная ошибка в search_metadata: {e}")
        return MetadataSearchResult(
            status    = ToolStatus.ERROR,
            tool_name = "search_metadata",
            query     = query,
            error_msg = str(e),
        )


# =============================================================
# Векторный поиск (основной путь)
# =============================================================

def _vector_search(
    retriever,
    query: str,
    top_k: int | None,
) -> MetadataSearchResult:
    """Выполняет поиск через ChromaDB."""
    chunks = retriever.search(query, top_k=top_k)

    if not chunks:
        return MetadataSearchResult(
            status    = ToolStatus.EMPTY,
            tool_name = "search_metadata",
            query     = query,
            chunks    = [],
        )

    logger.info(f"[vector_search] '{query}' -> {len(chunks)} результатов")

    return MetadataSearchResult(
        status    = ToolStatus.SUCCESS,
        tool_name = "search_metadata",
        query     = query,
        chunks    = chunks,
    )


# =============================================================
# SQL LIKE fallback (когда индекс не готов)
# =============================================================

# SQL-запрос для поиска по системным таблицам MS SQL
_SQL_SEARCH = """
SELECT DISTINCT
    t.name                                          AS table_name,
    CAST(ep_t.value AS NVARCHAR(MAX))               AS table_description,
    (
        SELECT STRING_AGG(c.name, ', ')
               WITHIN GROUP (ORDER BY c.column_id)
        FROM sys.columns c
        WHERE c.object_id = t.object_id
    )                                               AS columns_list,
    (
        SELECT COUNT(*)
        FROM sys.columns c
        WHERE c.object_id = t.object_id
          AND c.name LIKE ? ESCAPE '/'
    )                                               AS matching_columns_count
FROM sys.tables t
LEFT JOIN sys.extended_properties ep_t
    ON  ep_t.major_id = t.object_id
    AND ep_t.minor_id = 0
    AND ep_t.name     = 'MS_Description'
WHERE t.type = 'U'
  AND (
      t.name LIKE ? ESCAPE '/'
      OR CAST(ep_t.value AS NVARCHAR(MAX)) LIKE ? ESCAPE '/'
      OR EXISTS (
          SELECT 1 FROM sys.columns c2
          WHERE c2.object_id = t.object_id
            AND c2.name LIKE ? ESCAPE '/'
      )
  )
ORDER BY matching_columns_count DESC, t.name
"""


def _sql_fallback_search(
    query: str,
    top_k: int | None,
) -> MetadataSearchResult:
    """
    SQL LIKE поиск - fallback при недоступном векторном индексе.
    """
    from core.config import settings
    from core.db.connector import connector, ConnectorError

    limit    = top_k if top_k is not None else settings.rag_top_k
    keywords = _extract_keywords(query)
    all_chunks: list[MetadataChunk] = []

    for server in settings.servers:
        for db_config in server.databases:
            for keyword in keywords:
                like = f"%{_escape_like(keyword)}%"
                params = (like, like, like, like)

                try:
                    rows = connector.execute(
                        server.alias, db_config.name, _SQL_SEARCH, params=params
                    )
                except ConnectorError as e:
                    logger.warning(f"SQL fallback ошибка {server.alias}/{db_config.name}: {e}")
                    continue

                for row in rows:
                    columns = [
                        c.strip()
                        for c in (row.get("columns_list") or "").split(",")
                        if c.strip()
                    ]
                    raw_score = int(row.get("matching_columns_count") or 0)
                    score = min(raw_score / 5.0, 1.0)
                    if keyword.lower() in row["table_name"].lower():
                        score = min(score + 0.3, 1.0)

                    all_chunks.append(MetadataChunk(
                        table_name  = row["table_name"],
                        server      = server.alias,
                        database    = db_config.name,
                        description = str(row.get("table_description") or ""),
                        score       = round(score, 2),
                        columns     = columns,
                    ))

    if not all_chunks:
        return MetadataSearchResult(
            status=ToolStatus.EMPTY, tool_name="search_metadata",
            query=query, chunks=[],
        )

    # Дедупликация и сортировка
    seen: set[tuple] = set()
    unique: list[MetadataChunk] = []
    for c in all_chunks:
        key = (c.server, c.database, c.table_name)
        if key not in seen:
            seen.add(key)
            unique.append(c)

    unique.sort(key=lambda c: c.score, reverse=True)

    return MetadataSearchResult(
        status    = ToolStatus.SUCCESS,
        tool_name = "search_metadata",
        query     = query,
        chunks    = unique[:limit],
    )


def _extract_keywords(query: str) -> list[str]:
    """Простое извлечение ключевых слов (без стоп-слов)."""
    stop_words = {
        "где", "найти", "какая", "какой", "какие", "таблица", "таблицы",
        "структура", "схема", "скрипт", "напиши", "покажи", "дай",
        "нужен", "нужна", "по", "для", "в", "на", "из", "с", "и",
        "или", "а", "но", "не", "это", "есть", "как", "что", "кто",
        "запрос", "данные",
    }
    tokens = query.lower().replace("?", " ").replace(",", " ").split()
    keywords = [t for t in tokens if t not in stop_words and len(t) > 2]
    return keywords if keywords else [query]


def _escape_like(value: str) -> str:
    """Экранируем спецсимволы для LIKE."""
    return value.replace("/", "//").replace("%", "/%").replace("_", "/_")