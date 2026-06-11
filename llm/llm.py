"""
Унифицированная обёртка над LLM провайдерами.

Поддерживаемые провайдеры:
  - OpenRouter (ChatOpenAI с кастомным base_url) — для prod/облака
  - Ollama (ChatOllama) — для локальной разработки без интернета

Переключение через .env:
  USE_OLLAMA=false  → OpenRouter  (по умолчанию)
  USE_OLLAMA=true   → Ollama
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal

from langchain_core.language_models import BaseChatModel

from config import settings

logger = logging.getLogger(__name__)


# ===============================
# Внутренние фабрики провайдеров
# ===============================

def _make_openrouter(model_name: str, temperature: float) -> BaseChatModel:
    """
    ChatOpenAI, настроенный на OpenRouter.
    """
    from langchain_openai import ChatOpenAI

    if not settings.openrouter_api_key:
        raise ValueError(
            "OPENROUTER_API_KEY не задан в .env. "
            "Необходимо указать ключ "
            "или переключись на Ollama: USE_OLLAMA=true"
        )

    return ChatOpenAI(
        model           = model_name,
        openai_api_key  = settings.openrouter_api_key,
        openai_api_base = "https://openrouter.ai/api/v1",
        temperature     = temperature,
    )


def _make_ollama(model_name: str, temperature: float) -> BaseChatModel:
    """
    ChatOllama — локальный LLM-сервер.

    Установка модели перед использованием:
        ollama pull llama3.1:8b
        ollama pull deepseek-coder-v2:16b
    """
    try:
        from langchain_ollama import ChatOllama
    except ImportError:
        raise ImportError(
            "Ошибка при попытке иморта ChatOllama"
        )

    return ChatOllama(
        model       = model_name,
        base_url    = settings.ollama_host,
        temperature = temperature,
        reasoning   = settings.ollama_think,
        # Явно включаем JSON-mode — это важно для with_structured_output.
        # Без него Ollama иногда добавляет текст вокруг JSON.
        format      = "json",
        num_predict = 2048,   # хватит для SQL и классификации / ограничивает длину ответа (экономит RAM)
    )


# ===============================
# Обёртка (единый вход)
# ===============================

def make_llm(
    size:        Literal["small", "large"],
    temperature: float = 0.0,
) -> BaseChatModel:
    """
    Создаёт LLM нужного размера через активный провайдер.

    Args:
        size:        "small" — классификация и форматирование
                     "large" — генерация SQL
        temperature: 0.0 — детерминированные ответы

    Returns:
        BaseChatModel — работает одинаково независимо от провайдера.
    """
    model_name = settings.model_small if size == "small" else settings.model_large
    provider   = settings.active_provider

    logger.debug(f"LLM: provider={provider}, size={size}, model={model_name}")

    if settings.use_ollama:
        return _make_ollama(model_name, temperature)
    else:
        return _make_openrouter(model_name, temperature)


# ===============================
# Кешированные singleton-экземпляры
# ===============================
# lru_cache создаёт по одному экземпляру на (size, temperature).
# Это важно: каждый ChatOpenAI/ChatOllama держит HTTP-сессию,
# создавать новый на каждый вызов узла — расточительно.

@lru_cache(maxsize=4)
def get_llm(
    size:        Literal["small", "large"] = "small",
    temperature: float = 0.0,
) -> BaseChatModel:
    """
    Кешированный LLM клиент.
    """
    client = make_llm(size, temperature)
    logger.info(
        f"LLM инициализирован: provider={settings.active_provider}, "
        f"size={size}, model={settings.model_small if size == 'small' else settings.model_large}"
    )
    return client


# ===============================
# Health check: проверить что провайдер доступен
# ===============================

def check_provider() -> dict:
    """
    Пинг активного провайдера — полезно при старте приложения.

    Returns:
        {"provider": str, "model": str, "ok": bool, "error": str | None}
    """
    result = {
        "provider": settings.active_provider,
        "model":    settings.model_small,
        "ok":       False,
        "error":    None,
    }
    try:
        from langchain_core.messages import HumanMessage
        llm = get_llm("small")
        llm.invoke([HumanMessage(content="ping")])
        result["ok"] = True
    except Exception as e:
        result["error"] = str(e)
    return result