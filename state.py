#   =================================================== #
"""
МИНИМАЛЬНЫЙ STATE

Дальнейшие возможные расширения:
    - выбранный сервер;
    - выбранную БД;
    - confidence retrieval;
    - trace id из LangFuse;
    - structured citations по RAG-контексту;
    - ошибки выполнения DB tool.
"""
#   =================================================== #

from __future__ import annotations

from operator import add
from typing import Annotated, Any, Literal, TypedDict


# Возможные типы запросов (необходимо для классификации вопроса)
Intent = Literal[
    "schema_question",
    "business_search",
    "sql_generation",
    "live_db_question",
    "unsafe_request",
    "clarification_needed",
]

# статусы для сген-го SQL-скрипта
SqlValidationStatus = Literal["not_required", "safe", "unsafe", "ambiguous"]


class AgentState(TypedDict, total=False):
    """
    Минимальный State
    """

    question: str           # исходный вопрос пользователя
    intent: Intent          # тип запроса пользователя
    route_reason: str       # причина выбора ребра при классификации

    retrieved_context: list[dict[str, Any]]     # результат RAG
    generated_sql: str | None                   # сген-ый sql-скрипт
    sql_validation_status: SqlValidationStatus  # статус после валидации сген-го скрипта

    tool_results: Annotated[list[dict[str, Any]], add]      # трейс

    final_answer: str       # итоговый ответ
