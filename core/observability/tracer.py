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
    from core.config import settings
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key)


# Глобальный клиент LangFuse v3 инициализируем один раз на процесс.
_v3_client = None


def _ensure_v3_client():
    """Инициализирует глобальный клиент LangFuse v3 нашими ключами."""
    global _v3_client
    if _v3_client is None:
        from langfuse import Langfuse
        from core.config import settings
        _v3_client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    return _v3_client


def get_handler(
    session_id: str,
    user_query: str,
    tags: list[str] | None = None,
) -> object | None:
    """
    Создаёт LangFuse CallbackHandler для graph.invoke/stream
    (config={"callbacks": [handler]}).

    Совместимость по версиям SDK:
      - v3 
      - v2 
    """
    if not is_enabled():
        return None

    from core.config import settings

    # --- LangFuse v3 ------------------------------------------------------
    try:
        from langfuse.langchain import CallbackHandler  # есть только в v3
    except ImportError:
        CallbackHandler = None  # type: ignore[assignment]

    if CallbackHandler is not None:
        try:
            _ensure_v3_client()                 # креды -> глобальный клиент
            handler = CallbackHandler()         # без аргументов (v3 API)
            logger.info(f"LangFuse v3 handler создан (session={session_id[:8]}...)")
            return handler
        except Exception as e:  # noqa: BLE001
            logger.error(f"Ошибка создания LangFuse v3 handler: {e}")
            return None

    # --- LangFuse v2 (fallback) ------------------------------------------
    try:
        from langfuse.callback import CallbackHandler as CallbackHandlerV2
        handler = CallbackHandlerV2(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
            session_id=session_id,
            tags=tags or [],
        )
        logger.info(f"LangFuse v2 handler создан (session={session_id[:8]}...)")
        return handler
    except ImportError:
        logger.warning("langfuse не установлен.")
        return None
    except Exception as e:
        logger.error(f"Ошибка создания LangFuse v2 handler: {e}")
        return None


def flush(handler: object | None = None) -> None:
    """
    Принудительно отправляет накопленные события в LangFuse.

    v3: у хендлера метода flush нет.
    v2: flush есть у самого хендлера.
    """
    # v3
    try:
        from langfuse import get_client
        get_client().flush()
        return
    except Exception:  # noqa: BLE001
        pass

    # v2
    if handler is not None and hasattr(handler, "flush"):
        try:
            handler.flush()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"flush() недоступен: {e}")


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
        from core.config import settings

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