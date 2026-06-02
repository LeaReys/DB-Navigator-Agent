"""
Cборка графа LangGraph.

Два ветвления:
  1. После classify_intent → 6 веток по типу запроса
  2. Внутри DATA-ветки → execute или только вернуть SQL-скрипт
"""

from __future__ import annotations

from langgraph.graph import StateGraph, END

import sys
from pathlib import Path

from agent.state import AgentState
from schemes.models import QueryType, ToolStatus
from agent.nodes import (
    classify_intent,
    search_metadata_node,
    get_schema_node,
    generate_sql_node,
    execute_query_node,
    format_response_node,
    handle_unknown_node,
    unsafe_query_node,
)


# ===============================
# Функции-роутеры (определяют, какое ребро выбрать)
# ===============================

def route_by_query_type(state: AgentState) -> str:
    """
    Роутер 1: куда идти после классификации?
    Возвращает имя следующего узла.
    """
    classification = state.get("classification")
    if not classification:
        return "handle_unknown"

    route_map = {
        QueryType.NAVIGATION: "search_metadata",
        QueryType.SCHEMA:     "get_schema",
        QueryType.SCRIPT:     "generate_sql",
        QueryType.DATA:       "generate_sql",       # DATA тоже начинает с генерации SQL
        QueryType.UNKNOWN:    "handle_unknown",
        QueryType.UNSAFE:     "unsafe_query",
    }

    next_node = route_map.get(classification.query_type, "handle_unknown")
    print(f"[router-1] {classification.query_type} → {next_node}")
    return next_node


def route_after_sql_generation(state: AgentState) -> str:
    """
    Роутер 2: после генерации SQL — выполнять или нет?

    Выполняем только если:
      - тип запроса DATA (пользователь хочет реальные данные)
      - SQL успешно сгенерирован
      - SQL безопасный (только SELECT)
    """
    classification = state.get("classification")
    sql_result     = state.get("sql_result")

    is_data_query = (
        classification is not None
        and classification.query_type == QueryType.DATA
    )
    is_sql_ok = (
        sql_result is not None
        and sql_result.status == ToolStatus.SUCCESS
        and sql_result.generated is not None
        and sql_result.generated.is_safe
    )

    if is_data_query and is_sql_ok:
        print("[router-2] DATA + безопасный SQL → execute_query")
        return "execute_query"
    else:
        print("[router-2] SCRIPT или небезопасный SQL → format_response")
        return "format_response"


# ===============================
# Сборка графа
# ===============================

def build_graph() -> StateGraph:
    """
    Создаёт и компилирует граф агента.
    Возвращает скомпилированный граф, готовый к запуску.
    """
    graph = StateGraph(AgentState)

    # = Регистрируем узлы ==================
    graph.add_node("classify_intent",    classify_intent)
    graph.add_node("search_metadata",    search_metadata_node)
    graph.add_node("get_schema",         get_schema_node)
    graph.add_node("generate_sql",       generate_sql_node)
    graph.add_node("execute_query",      execute_query_node)
    graph.add_node("format_response",    format_response_node)
    graph.add_node("handle_unknown",     handle_unknown_node)
    graph.add_node("unsafe_query",       unsafe_query_node)

    # = Точка входа =====================
    graph.set_entry_point("classify_intent")

    # = Роутер 1: по типу запроса =============
    graph.add_conditional_edges(
        source="classify_intent",
        path=route_by_query_type,
        path_map={
            "search_metadata": "search_metadata",
            "get_schema":      "get_schema",
            "generate_sql":    "generate_sql",
            "handle_unknown":  "handle_unknown",
            "unsafe_query":    "unsafe_query",
        },
    )

    # = После RAG-поиска → сразу форматируем ответ ======
    graph.add_edge("search_metadata", "format_response")

    # = После получения схемы → сразу форматируем ответ ===
    graph.add_edge("get_schema", "format_response")

    # = Ротуер 2: после генерации SQL ===========
    graph.add_conditional_edges(
        source="generate_sql",
        path=route_after_sql_generation,
        path_map={
            "execute_query":  "execute_query",
            "format_response": "format_response",
        },
    )

    # = После выполнения SQL → форматируем ответ =======
    graph.add_edge("execute_query", "format_response")

    # = Финальные узлы → END =================
    graph.add_edge("format_response", END)
    graph.add_edge("handle_unknown",  END)
    graph.add_edge("unsafe_query",    END)
    return graph.compile()


# ===============================
# Запуск без трейсинга (для совместимости)
# ===============================

def run(user_query: str) -> AgentState:
    """Запускает граф и возвращает финальный стейт (без LangFuse)."""
    app = build_graph()

    initial_state: AgentState = {
        "user_query": user_query,
        "steps":      [],
    }

    final_state = app.invoke(initial_state)
    return final_state


# ===============================
# Запуск с LangFuse трейсингом (основной в production)
# ===============================

def run_traced(
    user_query: str,
    session_id: str,
    graph=None,
    tags: list[str] | None = None,
) -> AgentState:
    """
    Запускает граф с LangFuse трейсингом.
    """
    from observability.tracer import get_handler, flush
    import logging
    logger = logging.getLogger(__name__)

    if graph is None:
        graph = build_graph()

    initial_state: AgentState = {
        "user_query": user_query,
        "steps":      [],
    }

    handler = get_handler(session_id, user_query, tags=tags)

    if handler:
        config = {
            "callbacks": [handler],
            "run_name": "db-navigator-agent",
            "tags": tags or [],
            "metadata": {
                "langfuse_session_id": session_id,
                "query": user_query,
            },
        }

        final_state = graph.invoke(initial_state, config=config)
        flush(handler)
    else:
        logger.debug("LangFuse недоступен, запуск без трейсинга")
        final_state = graph.invoke(initial_state)

    return final_state