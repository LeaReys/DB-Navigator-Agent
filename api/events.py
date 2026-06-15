"""
Перевод обновлений узлов графа в события для фронтенда.

LangGraph в режиме stream_mode="updates" отдаёт по каждому отработавшему узлу
словарь вида {имя_узла: дельта_стейта}. Здесь мы превращаем эту дельту в
компактное JSON-событие, которое чат рисует как шаг на ленте графа.

Принцип: бэкенд решает, ЧТО показать (какой узел, какой tool, какие детали),
фронтенд решает, КАК показать. Никакой логики агента здесь нет —
только аккуратное извлечение полей из Pydantic-результатов узлов.
"""

from __future__ import annotations

from typing import Any


# Узлы, которые реально вызывают внешний инструмент (tool).
# Их фронтенд помечает значком TOOL — это и есть «вызовы tools».
TOOL_NODES: dict[str, str] = {
    "search_metadata": "search_metadata",
    "get_schema":      "get_table_schema",
    "execute_query":   "execute_query",
}

# Человекочитаемые подписи узлов (порядок = ожидаемый порядок на ленте).
NODE_LABELS: dict[str, str] = {
    "classify_intent": "Классификация запроса",
    "search_metadata": "RAG-поиск по метаданным",
    "get_schema":      "Чтение схемы таблицы",
    "generate_sql":    "Генерация SQL",
    "execute_query":   "Выполнение SQL",
    "fix_sql":         "Самоисправление SQL",
    "format_response": "Формирование ответа",
    "handle_unknown":  "Запрос вне домена БД",
    "unsafe_query":    "Блокировка небезопасного запроса",
}

# kind управляет цветом маркера на ленте:
#   tool   — вызов инструмента (БД/RAG)
#   llm    — шаг с обращением к модели
#   router — маршрутизация/терминальный узел
NODE_KIND: dict[str, str] = {
    "classify_intent": "llm",
    "search_metadata": "tool",
    "get_schema":      "tool",
    "generate_sql":    "llm",
    "execute_query":   "tool",
    "fix_sql":         "llm",
    "format_response": "llm",
    "handle_unknown":  "router",
    "unsafe_query":    "router",
}

# Узлы, после которых в стейте появляется финальный ответ.
TERMINAL_NODES = {"format_response", "handle_unknown", "unsafe_query"}


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Достаёт поле из Pydantic-модели или dict — что бы ни пришло из узла."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _status_str(obj: Any) -> str | None:
    """Нормализует ToolStatus (enum/строку) в строку."""
    status = _attr(obj, "status")
    if status is None:
        return None
    return getattr(status, "value", str(status))


def _truncate(text: str | None, limit: int = 600) -> str | None:
    if not text:
        return text
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def node_event(node: str, delta: dict[str, Any]) -> dict[str, Any] | None:
    """
    Строит событие 'step' из дельты одного узла.

    Возвращает None для терминальных узлов — для них отдельно
    собирается событие 'final' (см. final_event).
    """
    if node in TERMINAL_NODES:
        return None

    event: dict[str, Any] = {
        "type":  "step",
        "node":  node,
        "label": NODE_LABELS.get(node, node),
        "kind":  NODE_KIND.get(node, "router"),
        "tool":  TOOL_NODES.get(node),
        "status": "ok",
        "detail": {},
    }

    # --- Классификация --------------------------------------------------
    if node == "classify_intent":
        cls = delta.get("classification")
        qt = _attr(cls, "query_type")
        event["detail"] = {
            "query_type": getattr(qt, "value", str(qt)) if qt is not None else None,
            "confidence": _attr(cls, "confidence"),
            "reasoning":  _truncate(_attr(cls, "reasoning"), 240),
        }

    # --- RAG-поиск ------------------------------------------------------
    elif node == "search_metadata":
        res = delta.get("metadata_result")
        chunks = _attr(res, "chunks", []) or []
        event["status"] = "ok" if chunks else "warn"
        event["detail"] = {
            "found":  len(chunks),
            "tables": [_attr(c, "table_name") for c in chunks[:5]],
        }

    # --- Чтение схемы ---------------------------------------------------
    elif node == "get_schema":
        res = delta.get("schema_result")
        status = _status_str(res)
        cols = _attr(res, "columns", []) or []
        event["status"] = "ok" if status == "success" else "warn"
        event["detail"] = {
            "table":   _attr(res, "table"),
            "columns": len(cols),
            "status":  status,
        }

    # --- Генерация SQL --------------------------------------------------
    elif node == "generate_sql":
        res = delta.get("sql_result")
        gen = _attr(res, "generated")
        status = _status_str(res)
        event["status"] = "ok" if status == "success" else "error"
        event["detail"] = {
            "sql":      _truncate(_attr(gen, "sql")),
            "is_safe":  _attr(gen, "is_safe"),
            "status":   status,
        }

    # --- Самоисправление SQL -------------------------------------------
    elif node == "fix_sql":
        res = delta.get("sql_result")
        gen = _attr(res, "generated")
        status = _status_str(res)
        event["status"] = "warn"
        event["detail"] = {
            "attempt": delta.get("sql_retry_count"),
            "sql":     _truncate(_attr(gen, "sql")),
            "status":  status,
        }

    # --- Выполнение SQL -------------------------------------------------
    elif node == "execute_query":
        res = delta.get("execute_result")
        status = _status_str(res)
        event["status"] = {
            "success": "ok", "empty": "warn", "error": "error",
        }.get(status, "ok")
        event["detail"] = {
            "status":    status,
            "row_count": _attr(res, "row_count"),
            "truncated": _attr(res, "truncated"),
            "error":     _truncate(_attr(res, "error_msg"), 200),
        }

    return event


def final_event(
    final_response: Any,
    steps: list[str],
    elapsed_ms: int,
    tool_calls: int,
) -> dict[str, Any]:
    """Собирает финальное событие с готовым ответом агента."""
    qt = _attr(final_response, "query_type")
    sources = _attr(final_response, "sources", []) or []

    return {
        "type":       "final",
        "answer":     _attr(final_response, "answer", ""),
        "query_type": getattr(qt, "value", str(qt)) if qt is not None else "unknown",
        "confidence": _attr(final_response, "confidence", 0.0),
        "sql":        _attr(final_response, "sql"),
        "has_data":   _attr(final_response, "has_data", False),
        "sources": [
            {
                "server":   _attr(s, "server"),
                "database": _attr(s, "database"),
                "table":    _attr(s, "table"),
            }
            for s in sources
        ],
        "steps":      steps,
        "elapsed_ms": elapsed_ms,
        "tool_calls": tool_calls,
    }
