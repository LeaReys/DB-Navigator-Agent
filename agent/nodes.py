"""
Все узлы графа
"""

from __future__ import annotations
 
import logging
 
from langchain_core.messages import HumanMessage, SystemMessage
 
from agent.state import AgentState
from schemas.models import (
    QueryType,
    ClassificationResult,
    ToolStatus,
    SQLGenerationResult,
    GeneratedSQL,
    ExecuteQueryResult,
    AgentResponse,
    SourceReference,
)
from tools.metadata_search import search_metadata
from tools.schema_tool import get_table_schema
from tools.sql_tool import execute_query
from config import settings
 
logger = logging.getLogger(__name__)


# ===============================
# Вспомогательные функции
# ===============================

def _add_step(step: str) -> list[str]:
    """Возвращает список с одним шагом для Annotated[list, add] в стейте."""
    return [step]


def _resolve_target(state: AgentState) -> tuple[str, str]:
    """
    Определяет (server_alias, database) для execute_query.
    Приоритет: metadata_result → schema_result → первый из конфига.
    """
    if meta := state.get("metadata_result"):
        if meta.chunks:
            top = meta.chunks[0]
            return top.server, top.database
 
    if schema := state.get("schema_result"):
        if schema.server and schema.database:
            return schema.server, schema.database
 
    first = settings.servers[0]
    return first.alias, first.databases[0].name


# ===============================
# УЗЕЛ 1: Классификация запроса (роутер 1)
# ===============================
def classify_intent(state: AgentState) -> dict:
    """
    Классифицирует запрос через structured output малой LLM.
    При ошибке LLM — не роняем агента, возвращаем UNKNOWN.
    """
    from llm.llm import get_llm
    from llm.prompts import CLASSIFY_SYSTEM, CLASSIFY_USER
 
    query = state["user_query"]
    logger.info(f"[classify_intent] '{query}'")
 
    try:
        chain = get_llm("small").with_structured_output(ClassificationResult)
        result: ClassificationResult = chain.invoke([
            SystemMessage(content=CLASSIFY_SYSTEM),
            HumanMessage(content=CLASSIFY_USER.format(query=query)),
        ])
        logger.info(f"[classify_intent] → {result.query_type} (conf={result.confidence})")
 
    except Exception as e:
        logger.error(f"[classify_intent] LLM error: {e}")
        result = ClassificationResult(
            query_type        = QueryType.UNKNOWN,
            confidence        = 0.0,
            reasoning         = f"Ошибка классификации: {e}",
            mentioned_tables  = [],
            mentioned_entities= [],
        )
 
    return {
        "classification": result,
        "steps": _add_step(f"classify_intent:{result.query_type}"),
    }
 

# ===============================
# УЗЕЛ 2: RAG-поиск по метаданным (ветка NAVIGATION)
# ===============================
def search_metadata_node(state: AgentState) -> dict:
    """
    Поиск релевантных таблиц по запросу пользователя.
    Fallback на SQL LIKE.
    """
    query = state["user_query"]
    logger.info(f"[search_metadata] '{query}'")
 
    result = search_metadata(query)
 
    logger.info(f"[search_metadata] статус={result.status}, чанков={len(result.chunks)}")
    return {
        "metadata_result": result,
        "steps": _add_step("search_metadata"),
    }


# ===============================
# УЗЕЛ 3: Получение схемы таблицы (ветка SCHEMA)
# ===============================
def get_schema_node(state: AgentState) -> dict:
    """
    Получает структуру таблицы из MS SQL.

    Логика разрешения имени таблицы — два уровня:

    1. Классификатор → mentioned_tables[0].
       Работает когда пользователь называет таблицу точно: "структура debt".
       Но классификатор может вернуть бизнес-термин ("долг", "должник"),
       которого нет в БД — тогда get_table_schema вернёт EMPTY.

    2. RAG-fallback → search_metadata(query).
       Срабатывает в двух случаях:
         а) mentioned_tables пустой (пользователь не назвал таблицу явно);
         б) прямой поиск по mentioned_tables[0] не нашёл таблицу —
            значит, классификатор отдал бизнес-термин, RAG его разрешает.
    """
    from schemes.models import TableSchemaResult

    query          = state["user_query"]
    classification = state.get("classification")
    logger.info(f"[get_schema] '{query}'")

    mentioned = classification.mentioned_tables if classification else []

    def _resolve_via_rag() -> tuple[str, str, str] | None:
        """Ищет таблицу через RAG. Возвращает (table, server, database) или None."""
        meta = search_metadata(query, top_k=1)
        if meta.chunks:
            top = meta.chunks[0]
            return top.table_name, top.server, top.database
        return None

    if mentioned:
        # Сначала пробуем имя напрямую из классификатора
        table    = mentioned[0]
        server   = settings.servers[0].alias
        database = settings.servers[0].databases[0].name

        result = get_table_schema(server, database, table)

        # Если таблица не найдена — классификатор мог вернуть бизнес-термин
        # ("долг" вместо "debt"). Пробуем разрешить через RAG.
        if result.status in (ToolStatus.EMPTY, ToolStatus.ERROR):
            logger.info(
                f"[get_schema] '{table}' не найдена напрямую "
                f"(статус={result.status}), пробуем RAG"
            )
            resolved = _resolve_via_rag()
            if resolved:
                table, server, database = resolved
                logger.info(f"[get_schema] RAG разрешил: '{table}'")
                result = get_table_schema(server, database, table)
    else:
        # Таблица не названа явно — сразу идём в RAG
        resolved = _resolve_via_rag()
        if resolved:
            table, server, database = resolved
            result = get_table_schema(server, database, table)
        else:
            result = TableSchemaResult(
                status    = ToolStatus.EMPTY,
                tool_name = "get_table_schema",
                server    = "", database = "", table = "",
                error_msg = "Не удалось определить таблицу из запроса",
            )
            return {"schema_result": result, "steps": _add_step("get_schema:not_found")}

    logger.info(
        f"[get_schema] {result.table}: "
        f"{len(result.columns)} колонок, статус={result.status}"
    )
    return {"schema_result": result, "steps": _add_step("get_schema")}


# ===============================
# УЗЕЛ 4: Генерация SQL (ветки SCRIPT и DATA)
# ===============================
def generate_sql_node(state: AgentState) -> dict:
    """
    Генерирует SQL-скрипт по запросу пользователя.
    """
    from llm.llm import get_llm
    from llm.prompts import GENERATE_SQL_SYSTEM, GENERATE_SQL_USER, build_schema_context
 
    query = state["user_query"]
    logger.info(f"[generate_sql] '{query}'")
 
    # Обогащаем контекст если нет metadata_result (DATA-ветка)
    current_state = dict(state)
    if not current_state.get("metadata_result"):
        meta = search_metadata(query, top_k=3)
        if meta.chunks:
            current_state["metadata_result"] = meta
 
    schema_context = build_schema_context(current_state)
 
    try:
        chain = get_llm("large").with_structured_output(GeneratedSQL)
        generated: GeneratedSQL = chain.invoke([
            SystemMessage(content=GENERATE_SQL_SYSTEM),
            HumanMessage(content=GENERATE_SQL_USER.format(
                query=query,
                schema_context=schema_context,
            )),
        ])
        result = SQLGenerationResult(
            status=ToolStatus.SUCCESS, tool_name="generate_sql", generated=generated,
        )
        logger.info(f"[generate_sql] is_safe={generated.is_safe}, tables={generated.tables_used}")
 
    except ValueError as e:
        # Pydantic-валидатор поймал мутирующий оператор
        logger.warning(f"[generate_sql] unsafe SQL rejected: {e}")
        result = SQLGenerationResult(
            status=ToolStatus.ERROR, tool_name="generate_sql",
            error_msg=f"SQL содержит запрещённые операторы: {e}",
        )
    except Exception as e:
        logger.error(f"[generate_sql] LLM error: {e}")
        result = SQLGenerationResult(
            status=ToolStatus.ERROR, tool_name="generate_sql",
            error_msg=f"Ошибка генерации SQL: {e}",
        )
 
    return {
        "sql_result":      result,
        "metadata_result": current_state.get("metadata_result"),
        "steps":           _add_step(f"generate_sql:{result.status}"),
    }


# ===============================
# УЗЕЛ 5: Выполнение SQL (только ветка DATA, ветвление 2)
# ===============================
def execute_query_node(state: AgentState) -> dict:
    """
    Выполняет SELECT через pyodbc с лимитом строк и защитой от мутаций
    """
    sql_result = state.get("sql_result")
 
    if not sql_result or not sql_result.generated:
        return {
            "execute_result": ExecuteQueryResult(
                status=ToolStatus.ERROR, tool_name="execute_query",
                sql="", error_msg="SQL не был сгенерирован",
            ),
            "steps": _add_step("execute_query:no_sql"),
        }
 
    sql              = sql_result.generated.sql
    server, database = _resolve_target(state)
 
    logger.info(f"[execute_query] {server}/{database}")
    result = execute_query(server, database, sql)
    logger.info(f"[execute_query] статус={result.status}, строк={result.row_count}")
 
    return {
        "execute_result": result,
        "steps": _add_step(f"execute_query:{result.status}"),
    }


# ===============================
# УЗЕЛ 5.5: Самоисправление SQL (SQL self-correction loop)
# ===============================
def fix_sql_node(state: AgentState) -> dict:
    """
    Исправляет SQL-запрос после ошибки выполнения.

    Запускается только когда execute_query_node вернул статус ERROR
    и лимит попыток не исчерпан.
    """
    from llm.llm import get_llm
    from llm.prompts import FIX_SQL_SYSTEM, FIX_SQL_USER, build_schema_context

    query          = state["user_query"]
    sql_result     = state.get("sql_result")
    execute_result = state.get("execute_result")
    retry_count    = state.get("sql_retry_count", 0)

    failed_sql = (
        sql_result.generated.sql
        if sql_result and sql_result.generated
        else "— SQL отсутствует —"
    )
    error_msg = (
        execute_result.error_msg
        if execute_result and execute_result.error_msg
        else "неизвестная ошибка"
    )

    logger.info(
        f"[fix_sql] попытка {retry_count + 1}, "
        f"ошибка: {error_msg!r}"
    )

    schema_context = build_schema_context(state)

    try:
        chain = get_llm("large").with_structured_output(GeneratedSQL)
        fixed: GeneratedSQL = chain.invoke([
            SystemMessage(content=FIX_SQL_SYSTEM),
            HumanMessage(content=FIX_SQL_USER.format(
                query=query,
                schema_context=schema_context,
                failed_sql=failed_sql,
                error_msg=error_msg,
            )),
        ])
        result = SQLGenerationResult(
            status=ToolStatus.SUCCESS, tool_name="fix_sql", generated=fixed,
        )
        logger.info(f"[fix_sql] исправлен: {fixed.sql[:100].strip()}...")

    except ValueError as e:
        # Pydantic-валидатор поймал мутирующий оператор в «исправленном» SQL
        logger.warning(f"[fix_sql] unsafe SQL rejected: {e}")
        result = SQLGenerationResult(
            status=ToolStatus.ERROR, tool_name="fix_sql",
            error_msg=f"Исправленный SQL содержит запрещённые операторы: {e}",
        )
    except Exception as e:
        logger.error(f"[fix_sql] LLM error: {e}")
        result = SQLGenerationResult(
            status=ToolStatus.ERROR, tool_name="fix_sql",
            error_msg=f"Ошибка при исправлении SQL: {e}",
        )

    return {
        "sql_result":      result,
        "sql_retry_count": retry_count + 1,   # роутер проверит на следующем шаге
        "steps":           _add_step(f"fix_sql:attempt_{retry_count + 1}"),
    }


# ===============================
# УЗЕЛ 6: Обработка небезопасного запроса
# ===============================
def unsafe_query_node(state: AgentState) -> dict:
    """Блокирует запросы на изменение данных."""

    logger.warning(f"[unsafe_query] заблокирован: '{state.get('user_query')}'")
    final = AgentResponse(
        answer=(
            "Запрос заблокирован: агент работает только в режиме чтения.\n"
            "Операции INSERT, UPDATE, DELETE, DROP и подобные не поддерживаются."
        ),
        query_type=QueryType.UNSAFE,
        confidence=1.0,
    )
    return {"final_response": final, "steps": _add_step("unsafe_query:blocked")}


# ===============================
# УЗЕЛ 7: Форматирование финального ответа
# ===============================
def format_response_node(state: AgentState) -> dict:
    """
    Формирует финальный ответ через малую LLM.
    Fallback — собирает ответ из контекста без LLM.
    """
    from llm.llm import get_llm
    from llm.prompts import get_format_system, FORMAT_USER, build_results_context

    query          = state.get("user_query", "")
    classification = state.get("classification")
    query_type     = classification.query_type if classification else QueryType.UNKNOWN

    logger.info(f"[format_response] тип={query_type}")

    try:
        response = get_llm("small").invoke([
            SystemMessage(content=get_format_system(query_type)),   # ← тип-специфичный промпт
            HumanMessage(content=FORMAT_USER.format(
                query=query,
                query_type=str(query_type),                          # ← передаём тип в контекст
                results_context=build_results_context(state),
            )),
        ])
        answer = response.content.strip()
    except Exception as e:
        logger.error(f"[format_response] LLM error: {e}")
        answer = build_results_context(state)  # текстовый fallback
 
    # Собираем метаданные ответа
    sources: list[SourceReference] = []
    sql_text: str | None = None
    has_data = False
 
    if meta := state.get("metadata_result"):
        for chunk in meta.chunks:
            sources.append(SourceReference(
                server=chunk.server, database=chunk.database, table=chunk.table_name,
            ))
    if schema := state.get("schema_result"):
        if schema.server:
            sources.append(SourceReference(
                server=schema.server, database=schema.database, table=schema.table,
            ))
    if sql := state.get("sql_result"):
        if sql.generated:
            sql_text = sql.generated.sql
    if exec_r := state.get("execute_result"):
        has_data = exec_r.status == ToolStatus.SUCCESS
 
    final = AgentResponse(
        answer=answer, query_type=query_type, sql=sql_text,
        sources=sources,
        confidence=classification.confidence if classification else 0.0,
        has_data=has_data,
    )
    return {"final_response": final, "steps": _add_step("format_response")}

# ===============================
# УЗЕЛ-ЗАГЛУШКА: обработка неизвестного запроса
# ===============================

def handle_unknown_node(state: AgentState) -> dict:
    """Отвечает если запрос не относится к работе с БД."""
    logger.info(f"[handle_unknown] '{state.get('user_query')}'")
    final = AgentResponse(
        answer=(
            "Не смог понять запрос. Я помогаю только с вопросами по базам данных.\n\n"
            "Примеры:\n"
            "  • «Где хранится статус должника?»\n"
            "  • «Структура таблицы debt»\n"
            "  • «Напиши скрипт для последней даты платежа»\n"
            "  • «Какой статус у должника с id 123?»"
        ),
        query_type=QueryType.UNKNOWN,
        confidence=0.0,
    )
    return {"final_response": final, "steps": _add_step("handle_unknown")}