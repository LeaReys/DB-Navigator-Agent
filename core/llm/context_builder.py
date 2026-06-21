"""
Построители контекста для LLM-узлов агента.

Разделение ответственности с prompts.py:
  - prompts.py        - ЧТО говорить модели (статичные шаблоны промптов)
  - context_builder.py - КАК собрать контекст из AgentState в текст для этих шаблонов
"""

from __future__ import annotations

from core.schemas.models import ToolStatus

# =============================================================
# Системный промпт format_response: база + блок под тип запроса
# =============================================================

# Базовая часть - общая для всех типов запросов
_FORMAT_BASE = """\
Ты помощник backend-разработчика, который работает с базами данных MS SQL Server.
Отвечай на русском языке, кратко и по делу.
Не выдумывай информацию, которой нет в контексте."""

# Дополнительные инструкции под каждый тип - короткие и точные.
# Чем конкретнее правила, тем меньше «фантазирует» малая модель.
_FORMAT_BY_TYPE: dict[str, str] = {

    "navigation": """\
Пользователь ищет, где в БД хранится нужная информация.
Назови найденные таблицы по имени. Для каждой - 1 предложение: что в ней хранится и почему подходит.
Если таблиц несколько - покажи все.
Если ничего не найдено - скажи прямо и предложи переформулировать запрос.
Максимум 6 предложений.""",

    "schema": """\
Пользователь хочет узнать структуру таблицы.
Начни с одного предложения: для чего эта таблица.
Затем перечисли ключевые поля: имя, тип данных, и что означает.
Обязательно выдели PRIMARY KEY.
Если есть числовые поля-справочники (status, type, code) - отметь, что это коды, требующие расшифровки через словарь.
Максимум 8 предложений.""",

    "script": """\
SQL-скрипт сгенерирован и будет показан пользователю отдельно - не дублируй его в ответе.
Объясни в 2–3 предложениях что делает запрос: с какими таблицами работает, что фильтрует, что возвращает.
Если скрипт не был создан - объясни причину.""",

    "data": """\
Пользователь хочет получить конкретные данные из БД.
Дай прямой ответ на вопрос, используя данные из контекста.
Если записей несколько - выдели главное, не перечисляй всё построчно.
Если данных нет - скажи прямо.
Максимум 5 предложений.""",

    "unknown": """\
Запрос непонятен или не относится к работе с БД.
Вежливо скажи об этом и приведи 2–3 примера запросов, которые ты умеешь обрабатывать.""",
}


def get_format_system(query_type) -> str:
    """
    Возвращает системный промпт для форматирования ответа.

    Состоит из общей базы и короткого блока правил,
    специфичных для типа запроса.

    Args:
        query_type: QueryType enum или строка ("navigation", "schema", …)
    """
    key  = str(query_type)                          # работает и с enum, и со строкой
    addon = _FORMAT_BY_TYPE.get(key, _FORMAT_BY_TYPE["navigation"])
    return f"{_FORMAT_BASE}\n\n{addon}"


# =============================================================
# Построители текстового контекста из AgentState
# =============================================================

def build_schema_context(state: dict) -> str:
    """
    Строит текстовый контекст схемы для промпта generate_sql.
    Берёт данные из metadata_result и schema_result.
    """
    parts: list[str] = []

    if metadata_result := state.get("metadata_result"):
        if metadata_result.chunks:
            parts.append("Найденные таблицы (по поиску):")
            for chunk in metadata_result.chunks[:5]:
                cols = ", ".join(chunk.columns[:15])  # не больше 15 колонок
                parts.append(
                    f"  Таблица: {chunk.table_name} [{chunk.database}]\n"
                    f"  Описание: {chunk.description or 'нет'}\n"
                    f"  Колонки: {cols}"
                )

    if schema_result := state.get("schema_result"):
        if schema_result.columns:
            parts.append(f"\nДетальная схема таблицы {schema_result.table}:")
            for col in schema_result.columns:
                flags = []
                if col.is_pk:
                    flags.append("PK")
                if col.is_fk:
                    flags.append("FK")
                if col.is_nullable:
                    flags.append("NULL")
                flag_str = f" [{', '.join(flags)}]" if flags else ""
                desc = f": {col.description}" if col.description else ""
                parts.append(f"  - {col.name} ({col.data_type}){flag_str}{desc}")

    return "\n".join(parts) if parts else "Информация о схеме БД не найдена."


def build_results_context(state: dict) -> str:
    """
    Строит контекст результатов для промпта format_response.
    """
    parts: list[str] = []

    if metadata_result := state.get("metadata_result"):
        if metadata_result.chunks:
            tables = [
                f"{c.table_name} (score={c.score})"
                for c in metadata_result.chunks
            ]
            parts.append(f"Найденные таблицы: {', '.join(tables)}")

    if schema_result := state.get("schema_result"):
        if schema_result.columns:
            parts.append(
                f"Схема таблицы '{schema_result.table}': "
                f"{len(schema_result.columns)} колонок, "
                f"~{schema_result.row_count or '?'} строк"
            )

    if sql_result := state.get("sql_result"):
        if sql_result.generated:
            parts.append(f"SQL сгенерирован. {sql_result.generated.explanation}")

    if execute_result := state.get("execute_result"):
        if execute_result.status == ToolStatus.SUCCESS:
            rows = execute_result.rows[:5]
            rows_lines: list[str] = []
            for r in rows:
                pairs = ", ".join(f"{k}: {v}" for k, v in r.data.items())
                rows_lines.append(f"  • {pairs}")
            truncated = ", обрезано до 5" if execute_result.truncated else ""
            parts.append(
                f"Данные из БД ({execute_result.row_count} строк{truncated}):\n"
                + "\n".join(rows_lines)
            )
        elif execute_result.status == ToolStatus.EMPTY:
            parts.append("Запрос выполнен, но данных не найдено.")
        elif execute_result.error_msg:
            parts.append(f"Ошибка выполнения запроса: {execute_result.error_msg}")

    if state.get("error"):
        parts.append(f"Ошибка агента: {state['error']}")

    return "\n\n".join(parts) if parts else "Результатов нет."