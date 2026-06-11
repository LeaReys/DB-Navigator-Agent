"""
Тесты трёх роутеров графа — «мозга» агента, определяющего ветвление.
Роутеры чистые: принимают state-dict, возвращают имя следующего узла.
Живой граф/LLM/БД не нужны, но требуются установленные зависимости проекта
(pydantic, langgraph) — модели и agent.graph импортируются на уровне модуля.
"""

import pytest

from schemas.models import (
    QueryType,
    ClassificationResult,
    ToolStatus,
    SQLGenerationResult,
    GeneratedSQL,
    ExecuteQueryResult,
)
from agent.graph import (
    route_by_query_type,
    route_after_sql_generation,
    route_after_execute,
    MAX_SQL_RETRIES,
)


def _classification(query_type, confidence=0.9):
    return ClassificationResult(
        query_type=query_type, confidence=confidence, reasoning="test",
    )


# ===============================
# Роутер 1: route_by_query_type
# ===============================

@pytest.mark.parametrize("query_type,expected", [
    (QueryType.NAVIGATION, "search_metadata"),
    (QueryType.SCHEMA,     "get_schema"),
    (QueryType.SCRIPT,     "generate_sql"),
    (QueryType.DATA,       "generate_sql"),
    (QueryType.UNKNOWN,    "handle_unknown"),
    (QueryType.UNSAFE,     "unsafe_query"),
])
def test_route_by_query_type(query_type, expected):
    state = {"classification": _classification(query_type)}
    assert route_by_query_type(state) == expected


def test_route_by_query_type_without_classification_is_unknown():
    assert route_by_query_type({}) == "handle_unknown"


# ===============================
# Роутер 2: route_after_sql_generation
# ===============================

def _sql_result(is_safe=True):
    generated = GeneratedSQL(sql="SELECT 1", explanation="x", is_safe=is_safe)
    return SQLGenerationResult(status=ToolStatus.SUCCESS, generated=generated)


def test_router2_data_with_safe_sql_executes():
    state = {
        "classification": _classification(QueryType.DATA),
        "sql_result": _sql_result(is_safe=True),
    }
    assert route_after_sql_generation(state) == "execute_query"


def test_router2_script_does_not_execute():
    state = {
        "classification": _classification(QueryType.SCRIPT),
        "sql_result": _sql_result(is_safe=True),
    }
    assert route_after_sql_generation(state) == "format_response"


def test_router2_unsafe_sql_does_not_execute():
    state = {
        "classification": _classification(QueryType.DATA),
        "sql_result": _sql_result(is_safe=False),
    }
    assert route_after_sql_generation(state) == "format_response"


def test_router2_missing_sql_goes_to_format():
    state = {"classification": _classification(QueryType.DATA)}
    assert route_after_sql_generation(state) == "format_response"


# ===============================
# Роутер 3: route_after_execute
# ===============================

def _execute_result(status):
    return ExecuteQueryResult(status=status, sql="SELECT 1")


def test_router3_error_with_retries_left_goes_to_fix():
    state = {"execute_result": _execute_result(ToolStatus.ERROR), "sql_retry_count": 0}
    assert route_after_execute(state) == "fix_sql"


def test_router3_error_with_retries_exhausted_goes_to_format():
    state = {
        "execute_result": _execute_result(ToolStatus.ERROR),
        "sql_retry_count": MAX_SQL_RETRIES,
    }
    assert route_after_execute(state) == "format_response"


def test_router3_empty_is_not_treated_as_error():
    state = {"execute_result": _execute_result(ToolStatus.EMPTY), "sql_retry_count": 0}
    assert route_after_execute(state) == "format_response"


def test_router3_success_goes_to_format():
    state = {"execute_result": _execute_result(ToolStatus.SUCCESS), "sql_retry_count": 0}
    assert route_after_execute(state) == "format_response"
