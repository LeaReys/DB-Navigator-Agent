from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#   =================================================== #
# искусственный каталог БД
DUMMY_SCHEMA: list[dict[str, Any]] = [
    {
        "server": "demo_sql_server",
        "database": "collection_db",
        "schema": "dbo",
        "table": "person",
        "description": "Карточка должника: базовые идентификаторы и текущий бизнес-статус.",
        "business_terms": ["должник", "заемщик", "клиент", "person"],
        "columns": [
            {"name": "debtor_id", "type": "int", "nullable": False, "description": "ID должника", "key": "PK"},
            {"name": "full_name", "type": "nvarchar(255)", "nullable": False, "description": "ФИО должника"},
            {"name": "current_status_id", "type": "int", "nullable": False, "description": "Ссылка на актуальный статус", "key": "FK"},
            {"name": "updated_at", "type": "datetime2", "nullable": False, "description": "Дата последнего обновления"},
        ],
    },
    {
        "server": "demo_sql_server",
        "database": "collection_db",
        "schema": "dbo",
        "table": "debt",
        "description": "Задолженность должника: сумма, статус долга и даты изменения.",
        "business_terms": ["долг", "задолженность", "debt", "статус долга"],
        "columns": [
            {"name": "debt_id", "type": "int", "nullable": False, "description": "ID задолженности", "key": "PK"},
            {"name": "debtor_id", "type": "int", "nullable": False, "description": "Ссылка на должника", "key": "FK"},
            {"name": "status_id", "type": "int", "nullable": False, "description": "Ссылка на статус задолженности", "key": "FK"},
            {"name": "amount", "type": "decimal(18,2)", "nullable": False, "description": "Текущая сумма задолженности"},
            {"name": "updated_at", "type": "datetime2", "nullable": False, "description": "Дата последнего изменения"},
        ],
    },
    {
        "server": "demo_sql_server",
        "database": "collection_db",
        "schema": "dbo",
        "table": "debt_status",
        "description": "Справочник статусов задолженности и должника.",
        "business_terms": ["статус", "status", "справочник статусов"],
        "columns": [
            {"name": "status_id", "type": "int", "nullable": False, "description": "ID статуса", "key": "PK"},
            {"name": "status_code", "type": "varchar(50)", "nullable": False, "description": "Код статуса"},
            {"name": "status_name", "type": "nvarchar(255)", "nullable": False, "description": "Название статуса"},
        ],
    },
    {
        "server": "demo_sql_server",
        "database": "collection_db",
        "schema": "dbo",
        "table": "payment",
        "description": "Платежи должников: дата, сумма и связь с должником.",
        "business_terms": ["платеж", "платёж", "оплата", "payment", "последняя дата платежа"],
        "columns": [
            {"name": "payment_id", "type": "int", "nullable": False, "description": "ID платежа", "key": "PK"},
            {"name": "debtor_id", "type": "int", "nullable": False, "description": "Ссылка на должника", "key": "FK"},
            {"name": "payment_date", "type": "date", "nullable": False, "description": "Дата платежа"},
            {"name": "amount", "type": "decimal(18,2)", "nullable": False, "description": "Сумма платежа"},
        ],
    },
]

#   =================================================== #
# запрещенные SQL-действия
_FORBIDDEN_SQL_TOKENS = {
    "insert",
    "update",
    "delete",
    "merge",
    "drop",
    "alter",
    "truncate",
    "exec",
    "execute",
    "create",
}

#   =================================================== #

def _normalize(text: str) -> str:
    """
    Нормализация текста (при необходимости будет дополняться)
    """
    return text.lower().replace("ё", "е")


def schema_search_tool(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    """
    Поиск по фейковой БД, в будущем будет RAG
    """

    query_norm = _normalize(query)
    query_tokens = set(re.findall(r"[a-zа-я0-9_]+", query_norm))

    scored: list[tuple[int, dict[str, Any]]] = []
    for item in DUMMY_SCHEMA:
        searchable_parts = [
            item["table"],
            item["description"],
            " ".join(item["business_terms"]),
            " ".join(col["name"] + " " + col["description"] for col in item["columns"]),
        ]
        text = _normalize(" ".join(searchable_parts))
        score = sum(1 for token in query_tokens if token in text)

        # A few transparent demo boosts for common Russian business phrases.
        if "статус" in query_norm and item["table"] in {"debtor", "debt", "debt_status"}:
            score += 3
        if "должник" in query_norm and item["table"] in {"debtor", "debt", "payment"}:
            score += 2
        if "платеж" in query_norm and item["table"] == "payment":
            score += 4
        if item["table"] in query_norm:
            score += 5

        if score > 0:
            scored.append((score, item))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:top_k]]


def db_metadata_tool(table_name: str) -> dict[str, Any]:
    """
    Возвращает подробную структуру конкретной таблицы.
    """

    table_name_norm = _normalize(table_name)
    for item in DUMMY_SCHEMA:
        if item["table"] == table_name_norm:
            return {"found": True, "metadata": item}
    return {"found": False, "metadata": None, "message": f"Таблица '{table_name}' не найдена в БД."}


def validate_readonly_sql_tool(sql: str) -> dict[str, Any]:
    """
    Проверяет, что SQL безопасный.
    """

    sql_norm = _normalize(sql).strip()
    tokens = set(re.findall(r"[a-z_]+", sql_norm))

    if not sql_norm.startswith("select"):
        return {"status": "unsafe", "reason": "Только SELECT запросы доступны"}

    # ПРОВЕРКА: на запрещенные конструкции
    forbidden = sorted(tokens.intersection(_FORBIDDEN_SQL_TOKENS))
    if forbidden:
        return {"status": "unsafe", "reason": f"Обнаружены запрещённые SQL-команды: {forbidden}"}

    # ПРОВЕРКА: только один запрос в рамках скрипта
    without_final_semicolon = sql_norm[:-1] if sql_norm.endswith(";") else sql_norm
    if ";" in without_final_semicolon:
        return {"status": "unsafe", "reason": "Использование нескольких SQL-запросов одновременно не допускается."}

    # ПРОВЕРКА: Ограничение на вывод по кол-ву
    if "top" not in tokens and "fetch" not in tokens and "max(" not in sql_norm:
        return {
            "status": "ambiguous",
            "reason": "Запрос предназначен только для чтения, но должен содержать команды TOP/FETCH или агрегацию для ограничения количества строк.",
        }

    return {"status": "safe", "reason": "Запрос SELECT успешно прошёл проверку"}


def readonly_sql_tool(sql: str) -> list[dict[str, Any]]:
    """
    Имитирует выполнение SQL. (В проде будет EXEC)
    """

    # Дополнительно вызываем тул проверки запроса, дополнительный шаг по безопасности
    validation = validate_readonly_sql_tool(sql)
    if validation["status"] != "safe":
        return [{"error": validation["reason"]}]

    sql_norm = _normalize(sql)
    if "debtor_id = 123" in sql_norm and "status" in sql_norm:
        return [
            {
                "debtor_id": 123,
                "status_code": "ACTIVE_COLLECTION",
                "status_name": "Активное взыскание",
                "source": "dummy_rows",
            }
        ]

    if "max(" in sql_norm and "payment" in sql_norm:
        return [
            {"debtor_id": 123, "last_payment_date": "2026-04-15"},
            {"debtor_id": 456, "last_payment_date": "2026-03-29"},
        ]

    return []


def file_export_tool(content: str, path: str = "outputs/last_answer.txt") -> dict[str, Any]:
    """
    Сохраняем финальный ответ в файл. (В дальнейшем пригодится, если надо будет сохранять скрипт в файл или ответ в CSV/XLSX)
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": str(target), "bytes": target.stat().st_size}
