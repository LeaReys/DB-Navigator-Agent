"""
Индексация схемы БД в ChromaDB.
  1. Читает метаданные всех таблиц из MS SQL (sys.tables + sys.columns)
  2. Превращает каждую таблицу в текстовый документ (chunk)
  3. Загружает документы в ChromaDB (локальный векторный стор)

Когда запускать:
  - Первый запуск проекта
  - После значительных изменений в схеме БД
  - По расписанию (например, раз в неделю)

Индекс сохраняется на диск (chroma_persist_dir из конфига),
поэтому повторный запуск агента не переиндексирует БД заново.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

from db.connector import connector, ConnectorError
from config import settings, ServerConfig, DatabaseConfig

logger = logging.getLogger(__name__)

# Название коллекции в ChromaDB
COLLECTION_NAME = "db_schema"

# Модель эмбеддингов — локальная, не требует API-ключа.
# multilingual-e5-small: понимает русский и английский, весит ~120MB,
# работает быстро даже без GPU.
EMBEDDING_MODEL = "intfloat/multilingual-e5-small"


# =============================================================
# SQL для получения метаданных из MS SQL
# =============================================================

# Получаем все таблицы с описаниями
_SQL_TABLES = """
SELECT
    t.name                                          AS table_name,
    CAST(ep.value AS NVARCHAR(MAX))                 AS table_description,
    (
        SELECT COUNT(*)
        FROM sys.columns c
        WHERE c.object_id = t.object_id
    )                                               AS column_count
FROM sys.tables t
LEFT JOIN sys.extended_properties ep
    ON  ep.major_id = t.object_id
    AND ep.minor_id = 0
    AND ep.name     = 'MS_Description'
WHERE t.type = 'U'
ORDER BY t.name
"""

# Получаем колонки для одной таблицы
_SQL_COLUMNS_FOR_TABLE = """
SELECT
    c.name                                          AS column_name,
    tp.name                                         AS data_type,
    c.is_nullable,
    CAST(ep.value AS NVARCHAR(MAX))                 AS column_description
FROM sys.columns c
JOIN sys.types   tp  ON  tp.user_type_id = c.user_type_id
JOIN sys.objects obj ON  obj.object_id   = c.object_id
LEFT JOIN sys.extended_properties ep
    ON  ep.major_id = c.object_id
    AND ep.minor_id = c.column_id
    AND ep.name     = 'MS_Description'
WHERE obj.name = ?
  AND obj.type = 'U'
ORDER BY c.column_id
"""


# =============================================================
# Структура одного документа перед загрузкой в ChromaDB
# =============================================================

@dataclass
class SchemaDocument:
    """Текстовое представление таблицы для индексации."""
    doc_id:      str   # уникальный ID: "server__database__table"
    text:        str   # текст, который будет векторизован
    table_name:  str
    server:      str
    database:    str
    description: str
    columns:     str   # строка с перечнем колонок для метаданных


# =============================================================
# Публичный класс индексатора
# =============================================================

class SchemaIndexer:
    """
    Читает схему БД и загружает её в ChromaDB.
    """

    def __init__(self) -> None:
        self._client     = self._init_chroma()
        self._collection = self._get_or_create_collection()

    # == Инициализация ChromaDB ================================

    def _init_chroma(self) -> chromadb.PersistentClient:
        """Создаёт клиент ChromaDB."""
        client = chromadb.PersistentClient(path=settings.chroma_persist_dir)
        logger.info(f"ChromaDB: {settings.chroma_persist_dir}")
        return client

    def _get_or_create_collection(self) -> chromadb.Collection:
        """
        Возвращает или создаёт коллекцию в ChromaDB.
        """
        ef = SentenceTransformerEmbeddingFunction(
            model_name=EMBEDDING_MODEL,
        )
        collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"},  # косинусное расстояние
        )
        logger.info(f"Коллекция '{COLLECTION_NAME}': {collection.count()} документов")
        return collection

    # == Основной метод ========================================

    def run(self, force: bool = False) -> dict:
        """
        Запускает полную индексацию всех серверов и БД из конфига.

        Args:
            force: если True — удаляет старый индекс и строит заново.
                   По умолчанию пропускает уже проиндексированные таблицы.

        Returns:
            Словарь со статистикой: {"indexed": N, "skipped": N, "errors": N}
        """
        if force:
            logger.warning("force=True: удаляем старую коллекцию")
            self._client.delete_collection(COLLECTION_NAME)
            self._collection = self._get_or_create_collection()

        stats = {"indexed": 0, "skipped": 0, "errors": 0}

        for server in settings.servers:
            for db_config in server.databases:
                db_stats = self._index_database(server, db_config)
                for key in stats:
                    stats[key] += db_stats[key]

        logger.info(
            f"Индексация завершена: "
            f"проиндексировано={stats['indexed']}, "
            f"пропущено={stats['skipped']}, "
            f"ошибок={stats['errors']}"
        )
        return stats

    # == Индексация одной БД ===================================

    def _index_database(
        self,
        server:    ServerConfig,
        db_config: DatabaseConfig,
    ) -> dict:
        """Индексирует все таблицы одной базы данных."""
        stats = {"indexed": 0, "skipped": 0, "errors": 0}
        database = db_config.name

        logger.info(f"Индексируем: {server.alias}/{database}")

        try:
            tables = connector.execute(server.alias, database, _SQL_TABLES)
        except ConnectorError as e:
            logger.error(f"Ошибка подключения к {server.alias}/{database}: {e}")
            stats["errors"] += 1
            return stats

        # Если задан whitelist таблиц — фильтруем
        whitelist = set(db_config.tables_to_index)

        for table_row in tables:
            table_name = table_row["table_name"]

            if whitelist and table_name not in whitelist:
                stats["skipped"] += 1
                continue

            doc_id = f"{server.alias}__{database}__{table_name}"

            # Пропускаем уже проиндексированные (без force)
            existing = self._collection.get(ids=[doc_id])
            if existing["ids"]:
                logger.debug(f"Пропускаем (уже есть): {doc_id}")
                stats["skipped"] += 1
                continue

            # Получаем колонки таблицы
            try:
                columns_raw = connector.execute(
                    server.alias, database,
                    _SQL_COLUMNS_FOR_TABLE,
                    params=(table_name,),
                )
            except ConnectorError as e:
                logger.warning(f"Ошибка чтения колонок {table_name}: {e}")
                stats["errors"] += 1
                continue

            doc = self._build_document(
                table_name  = table_name,
                server      = server.alias,
                database    = database,
                table_desc  = table_row.get("table_description") or "",
                columns_raw = columns_raw,
            )

            self._upsert_document(doc)
            stats["indexed"] += 1
            logger.debug(f"Проиндексирована: {doc_id}")

        return stats

    # == Построение документа ==================================

    def _build_document(
        self,
        table_name:  str,
        server:      str,
        database:    str,
        table_desc:  str,
        columns_raw: list[dict],
    ) -> SchemaDocument:
        """
        Строит текстовый документ из метаданных таблицы.

        Качество RAG-поиска сильно зависит от того, как именно
        мы формируем текст для векторизации. Здесь используем
        структурированный шаблон, который хорошо работает
        с multilingual-e5-small.
        """
        # Формируем описание каждой колонки
        col_lines: list[str] = []
        col_names: list[str] = []

        for col in columns_raw:
            name    = col["column_name"]
            dtype   = col["data_type"]
            col_desc= col.get("column_description") or ""
            nullable= "NULL" if col["is_nullable"] else "NOT NULL"

            col_names.append(name)

            line = f"  - {name} ({dtype}, {nullable})"
            if col_desc:
                line += f": {col_desc}"
            col_lines.append(line)

        columns_text = "\n".join(col_lines)
        columns_str  = ", ".join(col_names)

        # Итоговый текст для векторизации.
        # Структура важна: сначала имя и описание (самое значимое),
        # потом перечень колонок.
        text = f"""Таблица: {table_name}
База данных: {database}
Сервер: {server}
Описание: {table_desc or 'нет описания'}

Колонки:
{columns_text}
""".strip()

        return SchemaDocument(
            doc_id      = f"{server}__{database}__{table_name}",
            text        = text,
            table_name  = table_name,
            server      = server,
            database    = database,
            description = table_desc,
            columns     = columns_str,
        )

    # == Сохранение в ChromaDB =================================

    def _upsert_document(self, doc: SchemaDocument) -> None:
        """Добавляет или обновляет документ в коллекции."""
        self._collection.upsert(
            ids        = [doc.doc_id],
            documents  = [doc.text],
            metadatas  = [{
                "table_name":  doc.table_name,
                "server":      doc.server,
                "database":    doc.database,
                "description": doc.description,
                "columns":     doc.columns,
            }],
        )

    # == Статус индекса ========================================

    def get_stats(self) -> dict:
        """Возвращает текущую статистику индекса."""
        count = self._collection.count()
        return {
            "total_documents": count,
            "collection":      COLLECTION_NAME,
            "persist_dir":     settings.chroma_persist_dir,
            "embedding_model": EMBEDDING_MODEL,
        }


# =============================================================
# CLI запуск: python -m rag.indexer
# =============================================================

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    print("=== DB Navigator — индексация схемы БД ===\n")

    indexer = SchemaIndexer()

    print("Текущий индекс:")
    print(json.dumps(indexer.get_stats(), indent=2, ensure_ascii=False))
    print()

    import sys
    force = "--force" in sys.argv

    print(f"Запускаем индексацию (force={force})...")
    stats = indexer.run(force=force)

    print(f"\nГотово:")
    print(f"  Проиндексировано: {stats['indexed']} таблиц")
    print(f"  Пропущено:        {stats['skipped']} таблиц")
    print(f"  Ошибок:           {stats['errors']}")
    print()

    print("Итог:")
    print(json.dumps(indexer.get_stats(), indent=2, ensure_ascii=False))