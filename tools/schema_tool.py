"""
Инструмент получения структуры таблицы.

Запрашивает метаданные из системных таблиц MS SQL:
  sys.columns      — колонки и их типы
  sys.indexes      — первичные ключи
  sys.foreign_keys — внешние ключи
  sys.dm_db_partition_stats — приблизительный счётчик строк
"""

from __future__ import annotations

import logging

from db.connector import connector, ConnectorError
from schemas.models import (
    ColumnInfo,
    TableSchemaResult,
    ToolStatus,
)

logger = logging.getLogger(__name__)


# =============================================================
# SQL-запросы к системным таблицам
# =============================================================

# Получаем все колонки таблицы с типами и nullable
_SQL_COLUMNS = """
SELECT
    c.name                                         AS column_name,
    tp.name                                        AS data_type,
    c.is_nullable,
    c.column_id,
    CASE WHEN c.max_length = -1 THEN 'MAX'
         WHEN tp.name IN ('nvarchar','nchar') THEN CAST(c.max_length/2 AS VARCHAR)
         WHEN tp.name IN ('varchar','char','varbinary','binary')
              THEN CAST(c.max_length AS VARCHAR)
         ELSE NULL
    END                                            AS char_max_length,
    c.precision,
    c.scale,
    OBJECT_DEFINITION(c.default_object_id)        AS default_value,
    ep.value                                       AS description
FROM sys.columns c
JOIN sys.types   tp ON tp.user_type_id = c.user_type_id
JOIN sys.objects obj ON obj.object_id = c.object_id
LEFT JOIN sys.extended_properties ep
    ON  ep.major_id   = c.object_id
    AND ep.minor_id   = c.column_id
    AND ep.name       = 'MS_Description'
WHERE obj.name   = ?
  AND obj.type   = 'U'
ORDER BY c.column_id
"""

# Первичные ключи таблицы
_SQL_PRIMARY_KEYS = """
SELECT c.name AS column_name
FROM sys.key_constraints kc
JOIN sys.index_columns   ic  ON  ic.object_id  = kc.parent_object_id
                             AND ic.index_id   = kc.unique_index_id
JOIN sys.columns         c   ON  c.object_id   = ic.object_id
                             AND c.column_id   = ic.column_id
JOIN sys.objects         obj ON  obj.object_id = kc.parent_object_id
WHERE kc.type = 'PK'
  AND obj.name = ?
"""

# Внешние ключи таблицы (какие колонки ссылаются на другие таблицы)
_SQL_FOREIGN_KEYS = """
SELECT
    c.name  AS column_name,
    rt.name AS referenced_table
FROM sys.foreign_key_columns fkc
JOIN sys.foreign_keys        fk  ON  fk.object_id       = fkc.constraint_object_id
JOIN sys.columns             c   ON  c.object_id        = fkc.parent_object_id
                                 AND c.column_id        = fkc.parent_column_id
JOIN sys.objects             pt  ON  pt.object_id       = fkc.parent_object_id
JOIN sys.objects             rt  ON  rt.object_id       = fkc.referenced_object_id
WHERE pt.name = ?
"""

# Приблизительное количество строк из статистики
_SQL_ROW_COUNT = """
SELECT SUM(p.rows) AS row_count
FROM sys.objects       obj
JOIN sys.partitions    p   ON  p.object_id = obj.object_id
                           AND p.index_id  IN (0, 1)
WHERE obj.name = ?
  AND obj.type = 'U'
"""


# =============================================================
# Публичная функция инструмента
# =============================================================

def get_table_schema(
    server_alias: str,
    database:     str,
    table:        str,
) -> TableSchemaResult:
    """
    Возвращает полную структуру таблицы из MS SQL.
    
    Args:
        server_alias: псевдоним сервера из конфига
        database:     имя базы данных
        table:        имя таблицы (без схемы, ищем в dbo)
    
    Returns:
        TableSchemaResult с полным списком колонок
    """
    logger.info(f"get_table_schema: {server_alias}/{database}/{table}")

    try:
        # 1. Получаем колонки
        raw_columns = connector.execute(
            server_alias, database, _SQL_COLUMNS, params=(table,)
        )

        if not raw_columns:
            return TableSchemaResult(
                status=ToolStatus.EMPTY,
                tool_name="get_table_schema",
                server=server_alias,
                database=database,
                table=table,
                error_msg=f"Таблица '{table}' не найдена в {database}",
            )

        # 2. Получаем PK и FK для аннотации колонок
        pk_columns = {
            row["column_name"]
            for row in connector.execute(
                server_alias, database, _SQL_PRIMARY_KEYS, params=(table,)
            )
        }

        fk_map: dict[str, str] = {
            row["column_name"]: row["referenced_table"]
            for row in connector.execute(
                server_alias, database, _SQL_FOREIGN_KEYS, params=(table,)
            )
        }

        # 3. Собираем ColumnInfo
        columns: list[ColumnInfo] = []
        for row in raw_columns:
            # Формируем строку типа: varchar(255), decimal(18,2), nvarchar(MAX)
            data_type = _format_type(row)

            columns.append(ColumnInfo(
                name        = row["column_name"],
                data_type   = data_type,
                is_nullable = bool(row["is_nullable"]),
                is_pk       = row["column_name"] in pk_columns,
                is_fk       = row["column_name"] in fk_map,
                default     = row.get("default_value"),
                description = row.get("description"),
            ))

        # 4. Приблизительный счётчик строк
        row_count = connector.execute_scalar(
            server_alias, database, _SQL_ROW_COUNT, params=(table,)
        )

        return TableSchemaResult(
            status    = ToolStatus.SUCCESS,
            tool_name = "get_table_schema",
            server    = server_alias,
            database  = database,
            table     = table,
            columns   = columns,
            row_count = int(row_count) if row_count else None,
        )

    except ConnectorError as e:
        logger.error(f"get_table_schema failed: {e}")
        return TableSchemaResult(
            status    = ToolStatus.ERROR,
            tool_name = "get_table_schema",
            server    = server_alias,
            database  = database,
            table     = table,
            error_msg = str(e),
        )


# =============================================================
# Вспомогательная функция
# =============================================================

def _format_type(row: dict) -> str:
    """
    Собирает читаемое имя типа из сырых данных sys.columns.
    
    Примеры: varchar(255), decimal(18,2), nvarchar(MAX), int, datetime
    """
    base = row["data_type"]

    if row.get("char_max_length"):
        return f"{base}({row['char_max_length']})"

    if base in ("decimal", "numeric") and row.get("precision"):
        return f"{base}({row['precision']},{row['scale']})"

    return base