"""
Инструмент выполнения SQL-запросов.
Принимает сгенерированный SQL, добавляет защиту и выполняет через коннектор.

Защитные слои:
  1. Pydantic-валидатор в GeneratedSQL (при генерации)
  2. DBConnector._check_query_safety (при передаче в коннектор)
  3. _inject_top_limit в этом файле — добавляем TOP N если его нет
"""

from __future__ import annotations

import logging
import re

from db.connector import connector, ConnectorError, UnsafeQueryError
from config import settings
from schemas.models import (
    ExecuteQueryResult,
    QueryRow,
    ToolStatus,
)

logger = logging.getLogger(__name__)


# =============================================================
# Публичная функция инструмента
# =============================================================

def execute_query(
    server_alias: str,
    database:     str,
    sql:          str,
    params:       tuple = (),
    max_rows:     int | None = None,
) -> ExecuteQueryResult:
    """
    Выполняет SELECT-запрос и возвращает результат.
    
    Args:
        server_alias: псевдоним сервера из конфига
        database:     имя базы данных
        sql:          SQL-запрос (только SELECT)
        params:       параметры подстановки (?, ?, ...) — защита от инъекций
        max_rows:     лимит строк в ответе
    
    Returns:
        ExecuteQueryResult с данными или описанием ошибки
    """
    limit = max_rows if max_rows is not None else settings.max_rows

    logger.info(f"execute_query: {server_alias}/{database}, limit={limit}")
    logger.debug(f"SQL: {sql}")

    # Добавляем TOP если его нет - чтобы случайно не вернуть миллион строк
    safe_sql, was_limited = _inject_top_limit(sql, limit)

    try:
        rows_raw = connector.execute(
            server_alias,
            database,
            safe_sql,
            params=params,
            max_rows=limit,
        )

        columns = list(rows_raw[0].keys()) if rows_raw else []
        rows    = [QueryRow(data=row) for row in rows_raw]

        return ExecuteQueryResult(
            status    = ToolStatus.SUCCESS if rows else ToolStatus.EMPTY,
            tool_name = "execute_query",
            sql       = sql,          # оригинальный SQL для отображения
            columns   = columns,
            rows      = rows,
            row_count = len(rows),
            truncated = was_limited and len(rows) == limit,
        )

    except UnsafeQueryError as e:
        logger.warning(f"Unsafe query blocked: {e}")
        return ExecuteQueryResult(
            status    = ToolStatus.ERROR,
            tool_name = "execute_query",
            sql       = sql,
            error_msg = f"Запрос заблокирован: {e}",
        )

    except ConnectorError as e:
        logger.error(f"execute_query failed: {e}")
        return ExecuteQueryResult(
            status    = ToolStatus.ERROR,
            tool_name = "execute_query",
            sql       = sql,
            error_msg = str(e),
        )


# =============================================================
# Вспомогательная функция
# =============================================================

def _inject_top_limit(sql: str, limit: int) -> tuple[str, bool]:
    """
    Добавляет TOP N в SELECT если его там нет.
    Возвращает (изменённый_sql, был_ли_добавлен_лимит).
    """
    # Ищем TOP в начале SELECT
    pattern = re.compile(
        r"^(\s*SELECT\s+)(TOP\s+\d+\s+)?",
        re.IGNORECASE,
    )

    match = pattern.match(sql)
    if not match:
        return sql, False

    existing_top = match.group(2)

    if existing_top:
        # TOP уже есть — проверяем что не превышает лимит
        existing_n = int(re.search(r"\d+", existing_top).group())
        if existing_n <= limit:
            return sql, False

        # Заменяем на наш лимит
        new_sql = pattern.sub(
            lambda m: m.group(1) + f"TOP {limit} ",
            sql,
        )
        logger.debug(f"TOP снижен с {existing_n} до {limit}")
        return new_sql, True

    # TOP отсутствует — добавляем
    new_sql = pattern.sub(
        lambda m: m.group(1) + f"TOP {limit} ",
        sql,
    )
    logger.debug(f"Добавлен TOP {limit}")
    return new_sql, True