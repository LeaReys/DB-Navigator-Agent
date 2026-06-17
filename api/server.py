"""
FastAPI-сервер DB Navigator Agent.

Эндпоинты:
  GET  /                 → чат-интерфейс (статика)
  GET  /api/health       → конфигурация и доступность LLM / БД / RAG / LangFuse
  POST /api/chat         → запуск агента со стримингом шагов (Server-Sent Events)

Сервер НЕ содержит логики агента: он переиспускает build_graph() из agent.graph
и стримит обновления узлов через web.events. Тот же граф работает в CLI (app.py),
поэтому поведение в браузере и в консоли совпадает, а трейсы по-прежнему уходят
в LangFuse через тот же callback-handler.

Запуск:
  uvicorn web.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator


from core.config import settings

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.events import node_event, final_event

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
for name in ("agent", "llm", "observability", "web", "core"):
    logging.getLogger(name).setLevel(logging.INFO)
logger = logging.getLogger("web.server")

STATIC_DIR = Path(__file__).parent / "static"

# Флаг готовности RAG: выставляется один раз на старте (warmup).
# health-эндпоинт читает его, не трогая модель эмбеддингов в потоке запроса.
_RAG_READY = False


def _warmup() -> None:
    """
    Однократный прогрев на старте приложения:
      1) строим RAG-индекс, если он пустой (модель эмбеддингов грузится ОДИН раз);
      2) собираем граф агента;
      3) открываем retriever (переиспользует уже загруженную модель).

    Любая ошибка здесь не валит сервер: агент умеет работать без RAG
    (SQL-fallback) и без предзагретого графа (соберётся лениво).
    """
    global _RAG_READY

    try:
        from core.rag.indexer import build_index_if_empty
        stats = build_index_if_empty()
        logger.info("Warmup: индексация — %s", stats)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Warmup: индексацию пропустили (%s). Будет SQL-fallback.", exc)

    try:
        get_graph()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Warmup: граф соберётся лениво (%s)", exc)

    try:
        from core.rag.retriever import get_retriever
        _RAG_READY = get_retriever().is_ready()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Warmup: retriever недоступен (%s)", exc)
        _RAG_READY = False

    logger.info("Warmup завершён. rag_ready=%s", _RAG_READY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Прогрев в отдельном потоке, чтобы не блокировать event loop старта.
    import anyio
    await anyio.to_thread.run_sync(_warmup)
    yield


app = FastAPI(title="DB Navigator Agent", docs_url="/api/docs", lifespan=lifespan)

# CORS открыт для удобства запуска фронтенда отдельно в dev.
# В проде список можно сузить до конкретного origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================
# Граф собираем один раз и переиспользуем между запросами
# =============================================================

_GRAPH = None


def get_graph():
    """Лениво собирает и кеширует скомпилированный граф."""
    global _GRAPH
    if _GRAPH is None:
        from core.agent.graph import build_graph
        logger.info("Собираем граф агента…")
        _GRAPH = build_graph()
    return _GRAPH


# =============================================================
# SSE-хелпер
# =============================================================

def _sse(payload: dict) -> str:
    """Кодирует объект в кадр Server-Sent Events."""
    data = json.dumps(payload, ensure_ascii=False, default=str)
    return f"data: {data}\n\n"


# =============================================================
# POST /api/chat — стриминг работы агента
# =============================================================

class ChatRequest(BaseModel):
    query: str
    session_id: str | None = None


def _run_stream(query: str, session_id: str) -> Iterator[str]:
    """
    Прогоняет граф в режиме updates и отдаёт каждый шаг как SSE-событие.

    Поток событий:
      run_start → step* → final → done
    либо при сбое:
      run_start → step* → error → done
    """
    from core.observability.tracer import get_handler, flush

    graph = get_graph()
    initial_state = {"user_query": query, "steps": []}

    handler = get_handler(session_id, query, tags=["web"])
    config = None
    if handler:
        config = {
            "callbacks": [handler],
            "run_name": "db-navigator-agent",
            "tags": ["web"],
            "metadata": {
                "langfuse_session_id": session_id,   # v3: сессия через metadata
                "langfuse_tags": ["web"],            # v3: теги через metadata
                "query": query,
            },
        }

    yield _sse({
        "type": "run_start",
        "session_id": session_id,
        "tracing": handler is not None,
    })

    t0 = time.perf_counter()
    steps_all: list[str] = []
    tool_calls = 0
    final_response = None

    try:
        stream = (
            graph.stream(initial_state, config=config, stream_mode="updates")
            if config else
            graph.stream(initial_state, stream_mode="updates")
        )
        for update in stream:
            for node, delta in update.items():
                if not isinstance(delta, dict):
                    continue
                new_steps = delta.get("steps")
                if new_steps:
                    steps_all.extend(new_steps)

                event = node_event(node, delta)
                if event:
                    if event.get("tool"):
                        tool_calls += 1
                    yield _sse(event)

                if delta.get("final_response") is not None:
                    final_response = delta["final_response"]

    except Exception as exc:  # noqa: BLE001 — наружу отдаём аккуратное событие
        logger.exception("Ошибка во время выполнения графа")
        yield _sse({"type": "error", "message": f"Сбой агента: {exc}"})
        yield _sse({"type": "done"})
        return

    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    if final_response is not None:
        yield _sse(final_event(final_response, steps_all, elapsed_ms, tool_calls))
    else:
        yield _sse({"type": "error", "message": "Агент не вернул финальный ответ"})

    if handler:
        try:
            flush(handler)
        except Exception:  # noqa: BLE001
            pass

    yield _sse({"type": "done"})


@app.post("/api/chat")
def chat(req: ChatRequest):
    """Запускает агента и стримит шаги через SSE."""
    query = (req.query or "").strip()
    session_id = req.session_id or str(uuid.uuid4())

    if not query:
        return JSONResponse({"error": "Пустой запрос"}, status_code=400)

    return StreamingResponse(
        _run_stream(query, session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # отключает буферизацию в nginx
        },
    )


# =============================================================
# GET /api/health — статус конфигурации и инфраструктуры
# =============================================================

@app.get("/api/health")
def health():
    """
    Лёгкая проверка окружения для шапки интерфейса.
    Живой пинг LLM не делаем (это стоит токенов) — только конфигурацию.
    """
    from core.config import settings

    result: dict = {
        "provider": settings.active_provider,
        "model_small": settings.model_small,
        "model_large": settings.model_large,
        "servers": [],
        "rag_ready": False,
        "langfuse": False,
    }

    # БД: быстрый SELECT 1 по каждому серверу
    try:
        from core.db.connector import connector
        for server in settings.servers:
            db_name = server.databases[0].name if server.databases else ""
            entry = {"alias": server.alias, "database": db_name, "ok": False}
            try:
                connector.execute(server.alias, db_name, "SELECT 1 AS ok")
                entry["ok"] = True
            except Exception as exc:  # noqa: BLE001
                entry["error"] = str(exc)[:200]
            result["servers"].append(entry)
    except Exception as exc:  # noqa: BLE001
        result["servers_error"] = str(exc)[:200]

    # RAG
    rag_ready = _RAG_READY
    try:
        from core.rag import retriever as _r
        if _r._retriever_instance is not None:
            rag_ready = _r._retriever_instance.is_ready()
    except Exception:  # noqa: BLE001
        pass
    result["rag_ready"] = bool(rag_ready)

    # LangFuse
    try:
        from core.observability.tracer import is_enabled
        result["langfuse"] = bool(is_enabled())
    except Exception:  # noqa: BLE001
        result["langfuse"] = False

    return result


# =============================================================
# Статика и корневая страница
# =============================================================

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")