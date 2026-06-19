"""
Тесты для nodes._derive_error — вычисление ошибки агента из result-объектов.

_derive_error — производная функция: она НЕ хранит ошибку, а вычисляет её
из sql_result / execute_result / schema_result (единый источник истины).
"""

import pytest

from core.schemas.models import (
    ToolStatus,
    SQLGenerationResult,
    ExecuteQueryResult,
    TableSchemaResult,
)
from core.agent.nodes import _derive_error


def _sql(status, error_msg=None):
    return SQLGenerationResult(status=status, tool_name="generate_sql", error_msg=error_msg)


def _exec(status, error_msg=None):
    return ExecuteQueryResult(status=status, tool_name="execute_query", error_msg=error_msg)


def _schema(status, error_msg=None):
    return TableSchemaResult(
        status=status, tool_name="get_table_schema",
        server="", database="", table="", error_msg=error_msg,
    )


def test_success_path_has_no_error():
    state = {"sql_result": _sql(ToolStatus.SUCCESS), "execute_result": _exec(ToolStatus.SUCCESS)}
    assert _derive_error(state) is None


def test_sql_generation_error():
    state = {"sql_result": _sql(ToolStatus.ERROR, "Ошибка генерации SQL: timeout")}
    assert _derive_error(state) == "Ошибка генерации SQL: timeout"


def test_execute_error():
    state = {
        "sql_result": _sql(ToolStatus.SUCCESS),
        "execute_result": _exec(ToolStatus.ERROR, "Invalid column name 'foo'"),
    }
    assert _derive_error(state) == "Invalid column name 'foo'"


def test_table_not_found():
    state = {"schema_result": _schema(ToolStatus.EMPTY, "Не удалось определить таблицу из запроса")}
    assert _derive_error(state) == "Не удалось определить таблицу из запроса"


def test_empty_result_without_error_is_not_an_error():
    state = {
        "sql_result": _sql(ToolStatus.SUCCESS),
        "execute_result": _exec(ToolStatus.EMPTY, None),
    }
    assert _derive_error(state) is None


def test_navigation_without_sql_has_no_error():
    assert _derive_error({}) is None


def test_error_status_without_message_gets_default():
    state = {"sql_result": _sql(ToolStatus.ERROR, None)}
    assert _derive_error(state) == "Ошибка генерации SQL"


def test_sql_error_takes_priority_over_execute():
    state = {
        "sql_result": _sql(ToolStatus.ERROR, "sql сломался"),
        "execute_result": _exec(ToolStatus.ERROR, "execute тоже"),
    }
    assert _derive_error(state) == "sql сломался"
