"""
Общий стейт графа LangGraph.

LangGraph передаёт этот объект от узла к узлу.
Каждый узел читает нужные поля и дописывает свои результаты.

Мы используем TypedDict (требование LangGraph), но внутри
храним Pydantic-модели из models.py — так получаем
и совместимость с LangGraph, и валидацию данных.
"""

from __future__ import annotations
from operator import add
from typing import Annotated, Any, Literal, TypedDict
from schemes.models import (
    ClassificationResult,
    MetadataSearchResult,
    TableSchemaResult,
    SQLGenerationResult,
    ExecuteQueryResult,
    AgentResponse,
)


class AgentState(TypedDict, total=False):
    """
    total=False означает, что все поля опциональны по умолчанию.
    Это нужно потому что LangGraph создаёт стейт постепенно —
    каждый узел добавляет только свои поля.
    """

    # = Вход =========================
    user_query: str                         # исходный вопрос пользователя

    # = Результаты узлов (заполняются по мере прохождения графа) =
    classification:  ClassificationResult   # выход роутера 1
    metadata_result: MetadataSearchResult   # выход RAG-поиска
    schema_result:   TableSchemaResult      # выход get_table_schema
    sql_result:      SQLGenerationResult    # выход generate_sql
    execute_result:  ExecuteQueryResult     # выход execute_query

    # = Финальный ответ ====================
    final_response:  AgentResponse

    # = Служебные поля ====================
    error: str | None             # текст ошибки, если что-то пошло не так
    steps: Annotated[list[str], add]     # трейс шагов