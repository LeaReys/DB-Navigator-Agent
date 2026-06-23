"""
Индексация схемы БД в ChromaDB.
  1. Читает метаданные всех таблиц из MS SQL (sys.tables + sys.columns)
  2. Обогащает каждый документ синонимами и связями из domain_knowledge.yaml
  3. Загружает документы в ChromaDB

Когда запускать:
  - Первый запуск проекта
  - После значительных изменений в схеме БД
  - После правки domain_knowledge.yaml  ← не забудьте --force
  - По расписанию (например, раз в неделю)

Индекс сохраняется на диск (chroma_persist_dir из конфига),
поэтому повторный запуск агента не переиндексирует БД заново.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
import chromadb

from core.db.connector import connector, ConnectorError
from core.config import settings, ServerConfig, DatabaseConfig
from core.rag.embeddings import COLLECTION_NAME, EMBEDDING_MODEL, get_embedding_function

logger = logging.getLogger(__name__)

# Путь к файлу доменных знаний (синонимы, связи, бизнес-описания)
_KNOWLEDGE_FILE = Path(__file__).with_name("domain_knowledge.yaml")


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
    text:        str   # текст для векторизации
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
        self._kb         = self._load_knowledge_base()  # ДОБАВЛЕНО: загружаем доменные знания

    # == Загрузка доменных знаний ==============================

    def _load_knowledge_base(self) -> dict:
        """
        Загружает domain_knowledge.yaml - файл с доп.знаниями о БД.
        Если файл не найден - работаем без него (только схема из БД).
        """
        if not _KNOWLEDGE_FILE.exists():
            logger.warning(
                f"Файл доменных знаний не найден: {_KNOWLEDGE_FILE}. "
                "Индексация пройдёт только по схеме БД."
            )
            return {}

        try:
            raw = yaml.safe_load(_KNOWLEDGE_FILE.read_text(encoding="utf-8"))
            tables_kb = raw.get("tables", {})
            logger.info(
                f"Доменные знания загружены: {_KNOWLEDGE_FILE.name}, "
                f"таблиц в файле: {len(tables_kb)}"
            )
            return tables_kb

        except Exception as e:
            logger.error(f"Ошибка чтения {_KNOWLEDGE_FILE}: {e}. Продолжаем без KB.")
            return {}

    # == Инициализация ChromaDB ================================

    def _init_chroma(self) -> chromadb.PersistentClient:
        """Создаёт клиент ChromaDB."""
        client = chromadb.PersistentClient(path=settings.chroma_persist_dir)
        logger.info(f"ChromaDB: {settings.chroma_persist_dir}")
        return client

    def _get_or_create_collection(self) -> chromadb.Collection:
        """
        Возвращает или создаёт коллекцию в ChromaDB.
        Эмбеддинг-функция берётся из общего модуля (модель грузится 1 раз).
        """
        collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=get_embedding_function(),
            metadata={"hnsw:space": "cosine"},  # косинусное расстояние
        )
        logger.info(f"Коллекция '{COLLECTION_NAME}': {collection.count()} документов")
        return collection

    # == Основной метод ========================================

    def run(self, force: bool = False) -> dict:
        """
        Запускает полную индексацию всех серверов и БД из конфига.

        Args:
            force: если True - удаляет старый индекс и строит заново.
                   По умолчанию пропускает уже проиндексированные таблицы.

                   Запускайте с --force всегда, когда:
                     - правили domain_knowledge.yaml
                     - добавили/изменили MS_Description в БД

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

        # Если задан whitelist таблиц - фильтруем
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
        Источники данных:
          - sys.tables / sys.columns / MS_Description → факты из БД
          - domain_knowledge.yaml → синонимы и связи (чего нет в схеме)
        """

        # --- Секция 1: колонки (из БД) ---
        col_lines: list[str] = []
        col_names: list[str] = []

        for col in columns_raw:
            name     = col["column_name"]
            dtype    = col["data_type"]
            col_desc = col.get("column_description") or ""
            nullable = "NULL" if col["is_nullable"] else "NOT NULL"

            col_names.append(name)
            line = f"  - {name} ({dtype}, {nullable})"
            if col_desc:
                line += f": {col_desc}"
            col_lines.append(line)

        columns_text = "\n".join(col_lines)
        columns_str  = ", ".join(col_names)

        # --- Секция 2: доменные знания из YAML ---
        kb = self._kb.get(table_name, {})
        synonyms     = kb.get("synonyms", [])
        biz_desc     = (kb.get("business_description") or "").strip()
        relationships = kb.get("relationships", [])
        enum_fields  = kb.get("enum_fields", {})

        # --- Сборка итогового текста ---
        lines: list[str] = [
            f"Таблица: {table_name}",
            f"База данных: {database}",
            f"Описание: {table_desc or 'нет описания'}",
        ]

        # Бизнес-смысл и синонимы - первыми после базовой инфо,
        # чтобы они весомо влияли на вектор
        if biz_desc:
            lines.append(f"Бизнес-смысл: {biz_desc}")
        if synonyms:
            lines.append(f"Синонимы и бизнес-термины: {', '.join(synonyms)}")

        # Связи - важны для SQL-генерации и навигационных запросов
        if relationships:
            lines.append("Связи с другими таблицами:")
            for rel in relationships:
                lines.append(f"  {rel}")

        # Enum-поля - если есть статусы/коды, описываем как их читать
        if enum_fields:
            lines.append("Поля-справочники (enum):")
            for field_name, field_info in enum_fields.items():
                desc = field_info.get("description", "")
                lines.append(f"  - {field_name}: {desc}")

        lines.append("Колонки:")
        lines.append(columns_text)

        text = "\n".join(lines)

        logger.debug(
            f"Документ '{table_name}': {len(col_names)} колонок, "
            f"синонимов={len(synonyms)}, связей={len(relationships)}"
        )

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
        """
        Добавляет или обновляет документ в коллекции.
        """
        self._collection.upsert(
            ids       = [doc.doc_id],
            documents = [f"passage: {doc.text}"],   # ← префикс e5
            metadatas = [{
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
            "knowledge_file":  str(_KNOWLEDGE_FILE),
            "kb_tables_count": len(self._kb),
        }


# =============================================================
# Удобный вызов из startup приложения
# =============================================================

def build_index_if_empty(force: bool = False) -> dict:
    """
    Строит индекс, только если он ещё пустой (или force=True).

    Вызывается на старте веб-приложения: в Docker база поднимается рядом,
    а готового chroma_db в образе нет - поэтому индекс строится один раз
    при первом запуске. Если БД недоступна - не падаем, а отдаём ошибку
    в статистике; агент продолжит работать через SQL-fallback.
    """
    indexer = SchemaIndexer()
    if not force and indexer.get_stats()["total_documents"] > 0:
        logger.info("RAG-индекс уже построен - пропускаем индексацию.")
        return {"indexed": 0, "skipped": 0, "errors": 0, "already_built": True}

    logger.info("RAG-индекс пуст - запускаем индексацию схемы БД…")
    return indexer.run(force=force)


# =============================================================
# CLI запуск: python -m core.rag.indexer
# =============================================================

if __name__ == "__main__":
    import json
    import sys

    from core.logging_config import setup_logging
    setup_logging()

    print("=== DB Navigator - индексация схемы БД ===\n")

    indexer = SchemaIndexer()

    print("Текущий индекс:")
    print(json.dumps(indexer.get_stats(), indent=2, ensure_ascii=False))
    print()

    force = "--force" in sys.argv

    print(f"Запускаем индексацию (force={force})...")
    stats = indexer.run(force=force)

    print("\nГотово:")
    print(f"  Проиндексировано: {stats['indexed']} таблиц")
    print(f"  Пропущено:        {stats['skipped']} таблиц")
    print(f"  Ошибок:           {stats['errors']}")
    print()

    print("Итог:")
    print(json.dumps(indexer.get_stats(), indent=2, ensure_ascii=False))