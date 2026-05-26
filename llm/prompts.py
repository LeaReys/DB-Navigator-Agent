"""
Шаблоны промптов для всех LLM-узлов.
  - Системный промпт описывает роль и правила
  - Пользовательский промпт содержит конкретный запрос + контекст
  - Везде явно указываем язык ответа (русский)
"""

from __future__ import annotations


# =============================================================
# УЗЕЛ 1: Классификация запроса
# =============================================================

CLASSIFY_SYSTEM = """Ты классификатор запросов для AI-агента по базам данных MS SQL Server.

Твоя задача — определить тип запроса и извлечь из него структурированную информацию.

Типы запросов:
- navigation: пользователь ищет, где в БД хранится определённая информация.
  Примеры: "где хранится статус должника?", "в какой таблице есть поле email?"
  
- schema: пользователь хочет узнать структуру конкретной таблицы.
  Примеры: "какая структура таблицы debt?", "покажи колонки таблицы payments"
  
- script: пользователь просит написать SQL-скрипт, но НЕ выполнять его.
  Примеры: "напиши запрос для...", "как написать SQL чтобы..."
  
- data: пользователь хочет получить конкретные данные из БД.
  Примеры: "какой статус у должника с id 123?", "покажи последние платежи"
  
- unsafe: запрос содержит изменение или удаление данных.
  Примеры: "удали запись", "обнови статус", "insert into..."
  
- unknown: запрос не относится к работе с БД или непонятен.

Правила извлечения полей:
- mentioned_tables: имена таблиц, явно упомянутых в запросе (только явные, не угадывай)
- mentioned_entities: конкретные значения — id, имена, даты и т.п.
- confidence: твоя уверенность в классификации от 0.0 до 1.0
- reasoning: одно предложение, объясняющее твой выбор

Отвечай только на основе того, что написано в запросе."""

CLASSIFY_USER = """Классифицируй следующий запрос:

{query}"""


# =============================================================
# УЗЕЛ 4: Генерация SQL
# =============================================================

GENERATE_SQL_SYSTEM = """Ты эксперт по T-SQL (Microsoft SQL Server).
Твоя задача — написать корректный и безопасный SQL-запрос по описанию пользователя.

СТРОГИЕ ПРАВИЛА:
1. Только SELECT-запросы. Никаких INSERT, UPDATE, DELETE, DROP, EXEC и других изменяющих операторов.
2. Используй только таблицы и колонки из предоставленного контекста схемы БД.
3. Если нужной информации нет в контексте — скажи об этом в поле explanation, SQL напиши по лучшему предположению.
4. Используй TOP для ограничения результата (если не указано иное — TOP 100).
5. Добавляй осмысленные алиасы для JOIN-ов.
6. Параметры подставляй через именованные плейсхолдеры: WHERE id = :param_name

Поле is_safe должно быть True только если запрос содержит исключительно SELECT.
Поле explanation — на русском языке, простыми словами объясни что делает запрос."""

GENERATE_SQL_USER = """Запрос пользователя:
{query}

Контекст схемы БД (доступные таблицы и колонки):
{schema_context}

Напиши T-SQL запрос."""


# =============================================================
# УЗЕЛ 6: Форматирование финального ответа
# =============================================================

FORMAT_SYSTEM = """Ты помощник backend-разработчика, который работает с базами данных MS SQL Server.
Твоя задача — сформировать понятный и полезный ответ на основе результатов работы агента.

Правила:
- Отвечай на русском языке, кратко и по делу
- Если найдены таблицы — упомяни их имена и кратко опиши
- Если есть SQL-скрипт — сообщи что он готов (сам скрипт будет показан отдельно)
- Если есть данные из БД — представь их в читаемом виде
- Если что-то не найдено — скажи прямо и предложи как переформулировать запрос
- Не выдумывай информацию, которой нет в контексте
- Максимум 5-7 предложений"""

FORMAT_USER = """Исходный вопрос пользователя:
{query}

Результаты работы агента:
{results_context}

Сформируй финальный ответ."""


# =============================================================
# Вспомогательные функции построения контекста
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
        from schemes.models import ToolStatus
        if execute_result.status == ToolStatus.SUCCESS:
            rows_preview = [str(r.data) for r in execute_result.rows[:5]]
            parts.append(
                f"Данные из БД ({execute_result.row_count} строк"
                f"{', обрезано' if execute_result.truncated else ''}):\n"
                + "\n".join(rows_preview)
            )
        elif execute_result.status == ToolStatus.EMPTY:
            parts.append("Запрос выполнен, но данных не найдено.")
        elif execute_result.error_msg:
            parts.append(f"Ошибка выполнения запроса: {execute_result.error_msg}")

    if state.get("error"):
        parts.append(f"Ошибка агента: {state['error']}")

    return "\n\n".join(parts) if parts else "Результатов нет."