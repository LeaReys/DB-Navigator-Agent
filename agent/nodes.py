"""
Все узлы графа LangGraph.

Сейчас узелы — заглушка: он принимает стейт,
печатает что делает, и возвращает стейт с тестовыми данными.
Реальная логика будет реализована далее.

Структура каждого узла:
    def node_name(state: AgentState) -> dict:
        ...
        return {"поле": значение, "steps": [...]}
        
"""

from __future__ import annotations

from agent.state import AgentState
from schemes.models import (
    QueryType,
    ClassificationResult,
    MetadataSearchResult,
    MetadataChunk,
    TableSchemaResult,
    ColumnInfo,
    ToolStatus,
    SQLGenerationResult,
    GeneratedSQL,
    ExecuteQueryResult,
    QueryRow,
    AgentResponse,
    SourceReference,
)


# ===============================
# Вспомогательная функция: добавить шаг в лог
# ===============================

def _add_step(state: AgentState, step: str) -> list[str]:
    """Возвращает список новых шагов (для слияния оператором add)."""
    return [step]


# ===============================
# УЗЕЛ 1: Классификация запроса (роутер 1)
# ===============================

def classify_intent(state: AgentState) -> dict:
    """
    Определяет тип запроса пользователя.
    
    Позже здесь будет вызов малой LLM 
    """
    query = state["user_query"]
    print(f"\n[classify_intent] Запрос: '{query}'")

    # = ЗАГЛУШКА: простая keyword-классификация для теста ==
    query_lower = query.lower()

    if any(kw in query_lower for kw   in ["удали", "измени", "очисти", "del", "upd"]):
        query_type = QueryType.UNSAFE
    elif any(kw in query_lower for kw in ["где", "найти", "в какой", "какая таблица"]):
        query_type = QueryType.NAVIGATION
    elif any(kw in query_lower for kw in ["структура", "колонки", "поля", "схема"]):
        query_type = QueryType.SCHEMA
    elif any(kw in query_lower for kw in ["напиши", "скрипт", "запрос", "sql"]):
        query_type = QueryType.SCRIPT
    elif any(kw in query_lower for kw in ["статус", "данные", "id", "покажи"]):
        query_type = QueryType.DATA
    else:
        query_type = QueryType.UNKNOWN

    result = ClassificationResult(
        query_type=query_type,
        confidence=0.2,
        reasoning=f"Определён тип {query_type} по ключевым словам",
        mentioned_tables=[],
        mentioned_entities=[],
    )

    print(f"[classify_intent] Тип: {result.query_type}, уверенность: {result.confidence}")

    return {
        "classification": result,
        "steps": _add_step(state, "classify_intent"),
    }


# ===============================
# УЗЕЛ 2: RAG-поиск по метаданным (ветка NAVIGATION)
# ===============================

def search_metadata_node(state: AgentState) -> dict:
    """
    RAG по метаданным БД.
    
    Позже здесь будет полноценный RAG
    """
    query = state["user_query"]
    print(f"\n[search_metadata] Ищем: '{query}'")

    # = ЗАГЛУШКА: возвращаем тестовый результат =======
    result = MetadataSearchResult(
        status=ToolStatus.SUCCESS,
        tool_name="search_metadata",
        query=query,
        chunks=[
            MetadataChunk(
                table_name="debt",
                server="PROD",
                database="ProdDB",
                description="Таблица должников. Хранит основные данные по долгу.",
                score=0.91,
                columns=["id", "debtor_id", "status", "amount", "created_at"],
            ),
            MetadataChunk(
                table_name="debtor_status_history",
                server="PROD",
                database="ProdDB",
                description="История смены статусов должника.",
                score=0.78,
                columns=["id", "debtor_id", "status", "changed_at"],
            ),
        ],
    )

    print(f"[search_metadata] Найдено чанков: {len(result.chunks)}")

    return {
        "metadata_result": result,
        "steps": _add_step(state, "search_metadata"),
    }


# ===============================
# УЗЕЛ 3: Получение схемы таблицы (ветка SCHEMA)
# ===============================

def get_schema_node(state: AgentState) -> dict:
    """
    Запрашивает структуру таблицы из MS SQL.
    """
    query = state["user_query"]
    print(f"\n[get_schema] Запрос схемы для: '{query}'")

    # = ЗАГЛУШКА =======================
    result = TableSchemaResult(
        status=ToolStatus.SUCCESS,
        tool_name="get_table_schema",
        server="PROD",
        database="ProdDB",
        table="debt",
        columns=[
            ColumnInfo(name="id",         data_type="int",          is_nullable=False, is_pk=True),
            ColumnInfo(name="debtor_id",  data_type="int",          is_nullable=False, is_fk=True),
            ColumnInfo(name="status",     data_type="varchar(50)",  is_nullable=False),
            ColumnInfo(name="amount",     data_type="decimal(18,2)",is_nullable=False),
            ColumnInfo(name="created_at", data_type="datetime",     is_nullable=False),
            ColumnInfo(name="updated_at", data_type="datetime",     is_nullable=True),
        ],
        row_count=150_420,
    )

    print(f"[get_schema] Таблица: {result.table}, колонок: {len(result.columns)}")

    return {
        "schema_result": result,
        "steps": _add_step(state, "get_schema"),
    }


# ===============================
# УЗЕЛ 4: Генерация SQL (ветки SCRIPT и DATA)
# ===============================

def generate_sql_node(state: AgentState) -> dict:
    """
    Генерирует SQL-скрипт по запросу пользователя.
    
    Позже здесь будет LLM для генерации
    """
    query = state["user_query"]
    print(f"\n[generate_sql] Генерируем SQL для: '{query}'")

    # = ЗАГЛУШКА =======================
    try:
        generated = GeneratedSQL(
            sql=(
                "SELECT d.id, d.status, MAX(p.payment_date) AS last_payment_date\n"
                "FROM debt d\n"
                "LEFT JOIN payments p ON p.debt_id = d.id\n"
                "WHERE d.debtor_id = :debtor_id\n"
                "GROUP BY d.id, d.status"
            ),
            explanation="Скрипт возвращает последнюю дату платежа в разрезе должника.",
            is_safe=True,
            tables_used=["debt", "payments"],
        )
        result = SQLGenerationResult(
            status=ToolStatus.SUCCESS,
            tool_name="generate_sql",
            generated=generated,
        )
    except ValueError as e:
        # Pydantic-валидатор поймает мутирующие операторы
        result = SQLGenerationResult(
            status=ToolStatus.ERROR,
            tool_name="generate_sql",
            error_msg=str(e),
        )

    print(f"[generate_sql] Статус: {result.status}")

    return {
        "sql_result": result,
        "steps": _add_step(state, "generate_sql"),
    }


# ===============================
# УЗЕЛ 5: Выполнение SQL (только ветка DATA, ветвление 2)
# ===============================

def execute_query_node(state: AgentState) -> dict:
    """
    Выполняет SQL-запрос через pyodbc и возвращает данные.
    """
    sql_result = state.get("sql_result")
    if not sql_result or not sql_result.generated:
        return {
            "execute_result": ExecuteQueryResult(
                status=ToolStatus.ERROR,
                tool_name="execute_query",
                sql="",
                error_msg="SQL не был сгенерирован",
            ),
            "steps": _add_step(state, "execute_query:error"),
        }

    sql = sql_result.generated.sql
    print(f"\n[execute_query] Выполняем:\n{sql}")

    # = ЗАГЛУШКА =======================
    result = ExecuteQueryResult(
        status=ToolStatus.SUCCESS,
        tool_name="execute_query",
        sql=sql,
        columns=["id", "status", "last_payment_date"],
        rows=[
            QueryRow(data={"id": 123, "status": "active", "last_payment_date": "2025-03-15"}),
        ],
        row_count=1,
        truncated=False,
    )

    print(f"[execute_query] Строк получено: {result.row_count}")

    return {
        "execute_result": result,
        "steps": _add_step(state, "execute_query"),
    }


# ===============================
# УЗЕЛ 6: Форматирование финального ответа
# ===============================

def format_response_node(state: AgentState) -> dict:
    """
    Собирает финальный ответ из результатов предыдущих узлов.
    
    Позже здесь будет LLM для красивого ответа.
    """
    print("\n[format_response] Формируем финальный ответ...")

    classification = state.get("classification")
    query_type = classification.query_type if classification else QueryType.UNKNOWN

    # Определяем источники и формируем текст ответа
    sources: list[SourceReference] = []
    answer_parts: list[str] = []
    sql_text: str | None = None
    has_data = False

    if metadata_result := state.get("metadata_result"):
        for chunk in metadata_result.chunks:
            answer_parts.append(
                f"• Таблица `{chunk.table_name}` ({chunk.database}): {chunk.description}"
            )
            sources.append(SourceReference(
                server=chunk.server,
                database=chunk.database,
                table=chunk.table_name,
            ))

    if schema_result := state.get("schema_result"):
        cols = ", ".join(f"`{c.name}` ({c.data_type})" for c in schema_result.columns)
        answer_parts.append(
            f"Таблица `{schema_result.table}` содержит {len(schema_result.columns)} колонок:\n{cols}"
        )
        sources.append(SourceReference(
            server=schema_result.server,
            database=schema_result.database,
            table=schema_result.table,
        ))

    if sql_result := state.get("sql_result"):
        if sql_result.generated:
            sql_text = sql_result.generated.sql
            answer_parts.append(f"SQL-скрипт сгенерирован.\n{sql_result.generated.explanation}")

    if execute_result := state.get("execute_result"):
        if execute_result.status == ToolStatus.SUCCESS:
            has_data = True
            rows_preview = execute_result.rows[:5]  # не больше 5 строк в ответ
            rows_text = "\n".join(str(r.data) for r in rows_preview)
            answer_parts.append(f"Результат ({execute_result.row_count} строк):\n{rows_text}")

    if state.get("error"):
        answer_parts.append(f"Ошибка: {state['error']}")

    answer = "\n\n".join(answer_parts) if answer_parts else "Не удалось сформировать ответ."

    final = AgentResponse(
        answer=answer,
        query_type=query_type,
        sql=sql_text,
        sources=sources,
        confidence=classification.confidence if classification else 0.0,
        has_data=has_data,
    )

    print(f"[format_response] Готово. Тип: {final.query_type}")

    return {
        "final_response": final,
        "steps": _add_step(state, "format_response"),
    }


def unsafe_query_node(state: AgentState) -> dict:
    """Отвечает пользователю, если запрос содержит запрещенный запрос."""
    print("\n[handle_unknown] Запрос не классифицирован")

    final = AgentResponse(
        answer=(
            "Запрос содержит запрещенные SQL-действия.\n"
            "Агенту разрешено выполнять запросы только на чтение."
        ),
        query_type=QueryType.UNSAFE,
        confidence=0.0,
    )

    return {
        "final_response": final,
        "steps": _add_step(state, "unsafe_query"),
    }

# ===============================
# УЗЕЛ-ЗАГЛУШКА: обработка неизвестного запроса
# ===============================

def handle_unknown_node(state: AgentState) -> dict:
    """Отвечает пользователю, если запрос не удалось классифицировать."""
    print("\n[handle_unknown] Запрос не классифицирован")

    final = AgentResponse(
        answer=(
            "Не смог понять запрос. Попробуй переформулировать.\n"
            "Примеры: 'Структура таблицы debt', 'Где хранится статус должника?', "
            "'Скрипт для последней даты платежа'"
        ),
        query_type=QueryType.UNKNOWN,
        confidence=0.0,
    )

    return {
        "final_response": final,
        "steps": _add_step(state, "handle_unknown"),
    }