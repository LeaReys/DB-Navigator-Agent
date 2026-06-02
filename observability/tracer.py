"""
LangFuse интеграция
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# =============================================================
# Основные функции
# =============================================================

def is_enabled() -> bool:
    """
    True если оба LangFuse ключа заданы в .env.
    Если False — агент работает нормально, просто без трейсинга.
    """
    from config import settings
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key)


def get_handler(
    session_id: str,
    user_query: str,
    tags: list[str] | None = None,
) -> object | None:
    """
    Создаёт и возвращает LangFuse CallbackHandler
    для передачи в graph.invoke(config={"callbacks": [handler]}).
    LangGraph сам вызывает его на каждом шаге.
    """
    if not is_enabled():
        return None

    try:
        from langfuse.langchain import CallbackHandler
        from config import settings

        handler = CallbackHandler(
            public_key  = settings.langfuse_public_key,
            secret_key  = settings.langfuse_secret_key,
            host        = settings.langfuse_host,
            session_id  = session_id,
            trace_name  = "db-navigator-agent",
            tags        = tags or [],
            metadata    = {
                "query":       user_query,
                "provider":    settings.active_provider,
                "model_small": settings.model_small,
                "model_large": settings.model_large,
            },
        )
        logger.info(f"LangFuse handler создан (session={session_id[:8]}...)")
        return handler

    except ImportError:
        logger.warning("langfuse не установлен.")
        return None

    except Exception as e:
        logger.error(f"Ошибка создания LangFuse handler: {e}")
        return None


def flush(handler: object | None) -> None:
    """
    Принудительно отправляет все накопленные события в LangFuse.

    Если не вызвать flush(), последние события могут не успеть 
    отправиться до завершения процесса и трейс окажется неполным.
    """
    if handler is None:
        return
    try:
        handler.flush()
    except Exception as e:
        logger.warning(f"flush error: {e}")


# =============================================================
# Health check (используется в app.py --check)
# =============================================================

def check_langfuse() -> dict:
    """
    Проверяет соединение с LangFuse.
    """
    result: dict = {
        "enabled": False,
        "ok":      False,
        "host":    "",
        "error":   None,
    }

    if not is_enabled():
        result["error"] = "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY не заданы в .env"
        return result

    try:
        from langfuse import Langfuse
        from config import settings

        result["enabled"] = True
        result["host"]    = settings.langfuse_host

        client = Langfuse(
            public_key = settings.langfuse_public_key,
            secret_key = settings.langfuse_secret_key,
            host       = settings.langfuse_host,
        )
        client.auth_check()   # кидает исключение если ключи неверные
        result["ok"] = True
        logger.info("[tracer] LangFuse auth_check OK")

    except ImportError:
        result["error"] = "langfuse не установлен."
    except Exception as e:
        result["error"] = str(e)

    return result