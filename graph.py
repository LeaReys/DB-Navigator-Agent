from __future__ import annotations

import re
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from app.state import AgentState
from app.tools import (
    db_metadata_tool,
    file_export_tool,
    readonly_sql_tool,
    schema_search_tool,
    validate_readonly_sql_tool,
)


def _extract_table_name(question: str, retrieved_context: list[dict[str, Any]]) -> str | None:
    """
    Пытается определить, о какой таблице спрашивает пользователь.
    """
    question_norm = question.lower().replace("ё", "е")
    for table in ["debtor", "debt", "debt_status", "payment"]:
        if table in question_norm:
            return table
    if retrieved_context:
        return retrieved_context[0]["table"]
    return None


def _extract_id(question: str) -> int | None:
    """
    Пытается определеить есть ли id  в запросе
    """
    match = re.search(r"(?:id|идентификатор[а-я ]*)\s*(\d+)", question.lower())
    if match:
        return int(match.group(1))
    match = re.search(r"\b\d+\b", question)
    return int(match.group(0)) if match else None


def classify_intent_node(state: AgentState) -> dict[str, Any]:
    """
    Нода классификации запроса
    """

    question = state["question"]
    q = question.lower().replace("ё", "е")

    unsafe_words = ["update", "delete", "insert", "drop", "alter", "truncate", "обнови", "удали", "измени", "вставь"]
    if any(word in q for word in unsafe_words):
        return {"intent": "unsafe_request", "route_reason": "Запрос содержит запрещенные требования."}

    if any(word in q for word in ["структура", "колонки", "поля", "что означает поле", "schema"]):
        return {"intent": "schema_question", "route_reason": "Вопрос о структуре таблицы"}

    if any(word in q for word in ["где найти", "в какой таблице", "какие таблицы", "найти информацию"]):
        return {"intent": "business_search", "route_reason": "Вопрос о хранении данных"}

    if any(word in q for word in ["sql", "скрипт", "запрос", "select"]):
        return {"intent": "sql_generation", "route_reason": "Запрос генерации SQL-скрипта"}

    if any(word in q for word in ["какой актуальный", "покажи", "получи", "значение"]):
        return {"intent": "live_db_question", "route_reason": "Пользователь запросил данные из БД"}

    return {"intent": "clarification_needed", "route_reason": "Не удалось классифицировать вопрос."}


def route_after_classify(state: AgentState) -> Literal["retrieve_schema", "ask_clarification", "refuse"]:
    """
    Роутинг 1: после классификации запроса
    """
    intent = state["intent"]
    if intent == "unsafe_request":
        return "refuse"
    if intent == "clarification_needed":
        return "ask_clarification"
    return "retrieve_schema"


def retrieve_schema_node(state: AgentState) -> dict[str, Any]:
    """
    Вызов RAG/Псевдо RAG
    """

    matches = schema_search_tool(state["question"], top_k=3)
    return {
        "retrieved_context": matches,
        "tool_results": [
            {
                "tool": "schema_search_tool",
                "args": {"query": state["question"], "top_k": 3},
                "result_count": len(matches),
            }
        ],
    }


def generate_answer_node(state: AgentState) -> dict[str, Any]:
    """
    Генерирует ответ или SQL используя контекст
    """

    question = state["question"]
    intent = state["intent"]
    context = state.get("retrieved_context", [])

    if intent == "schema_question":
        table_name = _extract_table_name(question, context)
        metadata_result = db_metadata_tool(table_name or "")
        tool_event = {
            "tool": "db_metadata_tool",
            "args": {"table_name": table_name},
            "found": metadata_result["found"],
        }

        if not metadata_result["found"]:
            return {
                "final_answer": "Не нашёл таблицу в БД. Нужно уточнить название таблицы.",
                "tool_results": [tool_event],
            }

        table = metadata_result["metadata"]
        columns = "\n".join(
            f"- {col['name']} ({col['type']}): {col['description']}"
            for col in table["columns"]
        )
        answer = (
            f"Таблица {table['schema']}.{table['table']} — {table['description']}\n\n"
            f"Основные колонки:\n{columns}"
        )
        return {"final_answer": answer, "tool_results": [tool_event]}

    if intent == "business_search":
        if not context:
            return {"final_answer": "Не нашёл подходящие таблицы в БД. Нужно уточнить бизнес-термин."}

        lines = []
        for item in context:
            lines.append(f"- {item['schema']}.{item['table']}: {item['description']}")
        answer = "Подходят следующие таблицы:\n" + "\n".join(lines)
        return {"final_answer": answer}

    if intent == "sql_generation":
        q = question.lower().replace("ё", "е")
        if "послед" in q and "платеж" in q:
            sql = """SELECT
    p.debtor_id,
    MAX(p.payment_date) AS last_payment_date
FROM dbo.payment AS p
GROUP BY p.debtor_id
ORDER BY last_payment_date DESC;"""
        else:
            sql = """SELECT TOP (100)
    d.debtor_id,
    d.full_name,
    ds.status_name
FROM dbo.debtor AS d
JOIN dbo.debt_status AS ds
    ON ds.status_id = d.current_status_id;"""
        return {"generated_sql": sql, "final_answer": f"Сгенерировал read-only SQL:\n```sql\n{sql}\n```"}

    if intent == "live_db_question":
        debtor_id = _extract_id(question)
        if debtor_id is None:
            return {"final_answer": "Для выполнения запроса нужен ID."}

        sql = f"""SELECT TOP (1)
    d.debtor_id,
    ds.status_code,
    ds.status_name
FROM dbo.debtor AS d
JOIN dbo.debt_status AS ds
    ON ds.status_id = d.current_status_id
WHERE d.debtor_id = {debtor_id};"""
        return {"generated_sql": sql}

    return {"final_answer": "Не удалось обработать запрос."}


def route_after_generate(state: AgentState) -> Literal["validate_sql", "save_answer"]:
    if state.get("generated_sql"):
        return "validate_sql"
    return "save_answer"


def validate_sql_node(state: AgentState) -> dict[str, Any]:
    """
    Вызывает тулу валидации SQL-скрипта
    """

    sql = state.get("generated_sql") or ""
    validation = validate_readonly_sql_tool(sql)
    return {
        "sql_validation_status": validation["status"],
        "tool_results": [
            {
                "tool": "validate_readonly_sql_tool",
                "args": {"sql": sql},
                "status": validation["status"],
                "reason": validation["reason"],
            }
        ],
    }


def route_after_validate(state: AgentState) -> Literal["execute_sql", "refuse", "save_answer"]:
    if state.get("sql_validation_status") != "safe":
        return "refuse"
    if state["intent"] == "live_db_question":
        return "execute_sql"
    return "save_answer"


def execute_sql_node(state: AgentState) -> dict[str, Any]:
    """
    Выполнение SQL-скрипта
    """

    sql = state.get("generated_sql") or ""
    rows = readonly_sql_tool(sql)

    if rows and "error" not in rows[0]:
        answer = f"Результат запроса: {rows}\n\nИспользованный SQL:\n```sql\n{sql}\n```"
    else:
        answer = f"В результате запроса не отобраны данные. SQL:\n```sql\n{sql}\n```"

    return {
        "final_answer": answer,
        "tool_results": [
            {
                "tool": "readonly_sql_tool",
                "args": {"sql": sql},
                "row_count": len(rows),
            }
        ],
    }


def refuse_node(state: AgentState) -> dict[str, Any]:
    reason = state.get("route_reason") or "SQL validation failed or request is unsafe."
    return {
        "final_answer": (
            "Не могу выполнить этот запрос. "
            "Разрешены только безопасные read-only SELECT-операции. "
            f"Причина: {reason}"
        )
    }


def ask_clarification_node(state: AgentState) -> dict[str, Any]:
    return {
        "final_answer": (
            "Не хватает контекста для точного ответа. Уточните, пожалуйста: "
            "нужна структура таблицы, поиск таблицы по бизнес-термину, генерация SQL или получение значения из БД?"
        )
    }


def save_answer_node(state: AgentState) -> dict[str, Any]:
    """Save final answer to file to demonstrate a filesystem tool."""

    export_result = file_export_tool(state.get("final_answer", ""))
    return {
        "tool_results": [
            {
                "tool": "file_export_tool",
                "args": {"path": export_result["path"]},
                "bytes": export_result["bytes"],
            }
        ]
    }


def build_graph():
    graph = StateGraph(AgentState)

    # Основные узлы
    graph.add_node("classify_intent", classify_intent_node)
    graph.add_node("retrieve_schema", retrieve_schema_node)
    graph.add_node("generate_answer", generate_answer_node)
    graph.add_node("validate_sql", validate_sql_node)
    graph.add_node("execute_sql", execute_sql_node)
    graph.add_node("refuse", refuse_node)
    graph.add_node("ask_clarification", ask_clarification_node)
    graph.add_node("save_answer", save_answer_node)

    # Начальная точка
    graph.add_edge(START, "classify_intent")
    # 1 роутинг: классифицируем запрос пользователя
    graph.add_conditional_edges(
        "classify_intent",
        route_after_classify,
        {
            "retrieve_schema": "retrieve_schema",
            "ask_clarification": "ask_clarification",
            "refuse": "refuse",
        },
    )
    graph.add_edge("retrieve_schema", "generate_answer")
    # 2 роутинг: нужна ли проверка SQL или сразу даём ответ
    graph.add_conditional_edges(
        "generate_answer",
        route_after_generate,
        {
            "validate_sql": "validate_sql",
            "save_answer": "save_answer",
        },
    )
    # 3 роутинг: нужно исполнить SQL, ответ или откат
    graph.add_conditional_edges(
        "validate_sql",
        route_after_validate,
        {
            "execute_sql": "execute_sql",
            "refuse": "refuse",
            "save_answer": "save_answer",
        },
    )
    graph.add_edge("execute_sql", "save_answer")
    graph.add_edge("refuse", "save_answer")
    graph.add_edge("ask_clarification", "save_answer")
    graph.add_edge("save_answer", END)

    return graph.compile()
