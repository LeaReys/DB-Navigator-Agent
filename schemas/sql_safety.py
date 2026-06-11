"""
Единый источник для проверки SQL на мутирующие операторы.

Защита остаётся многослойной (Pydantic-валидатор + проверка в коннекторе),
но опирается на одну и ту же константу, а не на две копии.

benchmark/evaluator.py использует свой, более узкий
паттерн и не зависит от этого модуля — это изолированный модуль оценки.
"""

from __future__ import annotations

import re

MUTATION_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE"
    r"|EXEC|EXECUTE|GRANT|REVOKE|MERGE)\b",
    re.IGNORECASE,
)


def find_mutations(sql: str) -> list[str]:
    """
    Возвращает список найденных мутирующих операторов (в верхнем регистре,
    без дублей, отсортированный). Пустой список — запрос безопасен.
    """
    found = MUTATION_PATTERN.findall(sql)
    return sorted({f.upper() for f in found})
