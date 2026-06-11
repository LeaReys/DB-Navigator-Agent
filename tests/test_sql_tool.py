"""
Тесты для tools/sql_tool.py::_inject_top_limit — авто-добавление TOP N.
Тестируется чистая функция, без подключения к БД.
"""

from tools.sql_tool import _inject_top_limit


def test_adds_top_when_missing():
    sql, limited = _inject_top_limit("SELECT * FROM t", 100)
    assert limited is True
    assert sql == "SELECT TOP 100 * FROM t"


def test_keeps_existing_top_within_limit():
    sql, limited = _inject_top_limit("SELECT TOP 10 * FROM t", 100)
    assert limited is False
    assert sql == "SELECT TOP 10 * FROM t"


def test_lowers_top_above_limit():
    sql, limited = _inject_top_limit("SELECT TOP 500 * FROM t", 100)
    assert limited is True
    assert sql == "SELECT TOP 100 * FROM t"


def test_lowercase_select_is_handled():
    sql, limited = _inject_top_limit("select * from t", 50)
    assert limited is True
    assert sql == "select TOP 50 * from t"


def test_non_select_is_unchanged():
    # CTE (WITH ...) не начинается с SELECT — функция оставляет как есть.
    sql, limited = _inject_top_limit("WITH c AS (SELECT 1) SELECT * FROM c", 100)
    assert limited is False
    assert sql.startswith("WITH")
