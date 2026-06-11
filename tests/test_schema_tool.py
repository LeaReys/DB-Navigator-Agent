"""
Тесты для tools/schema_tool.py::_format_type — сборка читаемого имени типа колонки.
Чистая функция, без БД.
"""

from tools.schema_tool import _format_type


def test_varchar_with_length():
    assert _format_type({"data_type": "varchar", "char_max_length": "255"}) == "varchar(255)"


def test_nvarchar_max():
    assert _format_type({"data_type": "nvarchar", "char_max_length": "MAX"}) == "nvarchar(MAX)"


def test_decimal_precision_and_scale():
    row = {"data_type": "decimal", "char_max_length": None, "precision": 18, "scale": 2}
    assert _format_type(row) == "decimal(18,2)"


def test_plain_type_without_params():
    assert _format_type({"data_type": "int", "char_max_length": None}) == "int"
