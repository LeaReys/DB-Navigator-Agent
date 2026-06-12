"""
Тесты для tools/metadata_search.py — извлечение ключевых слов и экранирование LIKE.
Чистые функции, без БД.
"""

from core.tools.metadata_search import _extract_keywords, _escape_like


def test_extract_drops_stopwords_and_short_tokens():
    kws = _extract_keywords("Где хранится статус должника?")
    assert "где" not in kws          # стоп-слово
    assert "статус" in kws
    assert "должника" in kws


def test_extract_falls_back_to_full_query_when_all_stopwords():
    # если после фильтрации ничего не осталось — возвращаем исходный запрос целиком
    assert _extract_keywords("где найти") == ["где найти"]


def test_extract_strips_punctuation():
    assert "статус" in _extract_keywords("статус?")


def test_escape_like_escapes_special_characters():
    # порядок важен: '/' экранируется первым
    assert _escape_like("a_b%c/d") == "a/_b/%c//d"


def test_escape_like_plain_text_unchanged():
    assert _escape_like("debt") == "debt"
