"""
Тесты Pydantic-валидатора GeneratedSQL — первый рубеж защиты от мутаций.
Требует установленного pydantic.
"""

import pytest
from pydantic import ValidationError

from schemas.models import GeneratedSQL


def test_select_is_allowed():
    g = GeneratedSQL(sql="SELECT * FROM debt", explanation="x", is_safe=True)
    assert g.sql.startswith("SELECT")


def test_mutation_is_rejected_by_validator():
    with pytest.raises(ValidationError):
        GeneratedSQL(sql="DELETE FROM debt", explanation="x", is_safe=False)


def test_column_name_with_keyword_substring_is_allowed():
    # граница слова: R_CREATE_USER_ID не должно блокироваться
    g = GeneratedSQL(sql="SELECT R_CREATE_USER_ID FROM t", explanation="x", is_safe=True)
    assert "R_CREATE_USER_ID" in g.sql
