"""
Тесты для schemas/sql_safety.py — общего паттерна проверки SQL на мутации.
Чистый stdlib, без БД и LLM.
"""

from core.schemas.sql_safety import find_mutations, MUTATION_PATTERN


def test_safe_select_has_no_mutations():
    assert find_mutations("SELECT TOP 10 * FROM debt") == []


def test_each_mutation_keyword_detected():
    keywords = [
        "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER",
        "CREATE", "EXEC", "EXECUTE", "GRANT", "REVOKE", "MERGE",
    ]
    for kw in keywords:
        assert kw in find_mutations(f"{kw} something"), kw


def test_case_insensitive():
    assert find_mutations("delete from t") == ["DELETE"]


def test_result_is_sorted_unique_uppercase():
    # дубликаты схлопываются, регистр приводится к верхнему, порядок — сортировка
    assert find_mutations("update x; insert y; UPDATE z; drop a") == [
        "DROP", "INSERT", "UPDATE",
    ]


def test_word_boundary_no_false_positive_on_column_names():
    # имена колонок не должны ложно срабатывать (граница слова \b...\b)
    assert find_mutations("SELECT R_CREATE_USER_ID, UPDATED_AT FROM t") == []


def test_pattern_object_is_usable():
    assert MUTATION_PATTERN.search("DROP TABLE x") is not None
    assert MUTATION_PATTERN.search("SELECT 1") is None
