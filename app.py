"""
app.py = точка входа DB Navigator Agent.

Режимы:
  python app.py                  → интерактивный REPL
  python app.py "вопрос"         → одиночный запрос
  python app.py --bench          → полный benchmark (все 12 кейсов)
  python app.py --bench schema   → benchmark одной категории
  python app.py --check          → health check (LLM / БД / RAG / LangFuse)
  python app.py --help
"""

from __future__ import annotations

import sys
import time
import uuid
import logging
import textwrap

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logging.getLogger("agent").setLevel(logging.INFO)
logging.getLogger("llm").setLevel(logging.INFO)
logging.getLogger("observability").setLevel(logging.INFO)


# =============================================================
# ANSI-цвета
# =============================================================

class C:
    RESET  = "\033[0m";  BOLD   = "\033[1m";  DIM    = "\033[2m"
    CYAN   = "\033[36m"; GREEN  = "\033[32m";  YELLOW = "\033[33m"
    RED    = "\033[31m"

def _c(color, text): return f"{color}{text}{C.RESET}"
def _section(label): return f"\n{C.BOLD}{C.YELLOW}▶ {label}{C.RESET}"

_TYPE_ICONS = {
    "navigation": "🔍", "schema": "📋", "script": "📝",
    "data": "📊",       "unsafe": "🚫", "unknown": "❓",
}


# =============================================================
# Вывод одного результата
# =============================================================

def print_result(state: dict, elapsed: float) -> None:
    final = state.get("final_response")
    if not final:
        print(_c(C.RED, "  [ошибка] нет final_response в стейте")); return

    qt         = str(final.query_type)
    icon       = _TYPE_ICONS.get(qt, "•")
    conf_color = C.GREEN if final.confidence >= 0.7 else C.YELLOW if final.confidence >= 0.4 else C.RED

    print(f"\n  {icon}  Тип: {_c(C.BOLD, qt)}"
          f"  |  Уверенность: {_c(conf_color, f'{final.confidence:.0%}')}"
          f"  |  Время: {_c(C.DIM, f'{elapsed:.1f}s')}")

    steps = state.get("steps", [])
    if steps:
        print(f"  {_c(C.DIM, '  →  '.join(steps))}")

    print(_section("Ответ"))
    for line in final.answer.splitlines():
        print(f"  {line}")

    if final.sql:
        print(_section("SQL"))
        for line in final.sql.splitlines():
            print(f"  {_c(C.CYAN, line)}")

    if final.sources:
        print(_section("Источники"))
        seen: set[str] = set()
        for src in final.sources:
            key = f"{src.server}/{src.database}/{src.table or ''}"
            if key not in seen:
                seen.add(key)
                tbl = f".{_c(C.BOLD, src.table)}" if src.table else ""
                print(f"  {_c(C.DIM, src.server)} / {src.database}{tbl}")


# =============================================================
# Режим 1: интерактивный REPL
# =============================================================

def run_repl() -> None:
    from agent.graph import build_graph, run_traced
    from config import settings
    from observability.tracer import is_enabled

    graph      = build_graph()
    session_id = str(uuid.uuid4())

    print(_c(C.BOLD, "\n╔══════════════════════════════════════════╗"))
    print(_c(C.BOLD,   "║     DB Navigator = интерактивный режим   ║"))
    print(_c(C.BOLD,   "╚══════════════════════════════════════════╝"))
    print(f"  Провайдер : {_c(C.GREEN, settings.active_provider)}"
          f"  |  {_c(C.DIM, settings.model_small)} / {_c(C.DIM, settings.model_large)}")
    lf = _c(C.GREEN, "✓ LangFuse") if is_enabled() else _c(C.DIM, "LangFuse выключен")
    print(f"  {lf}  |  session={_c(C.DIM, session_id[:8])}...")
    print(f"  {_c(C.DIM, 'exit / quit = выход')}\n")

    while True:
        try:
            query = input(f"{_c(C.BOLD, _c(C.CYAN, '▶ Вопрос:'))} ").strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{_c(C.DIM, 'Выход.')}"); break
        if not query:
            continue
        if query.lower() in {"exit", "quit", "q"}:
            print(_c(C.DIM, "Выход.")); break

        _run_single(graph, query, session_id)


# =============================================================
# Режим 2: одиночный запрос
# =============================================================

def run_single(query: str) -> None:
    from agent.graph import build_graph
    graph = build_graph()
    _run_single(graph, query, str(uuid.uuid4()))


def _run_single(graph, query: str, session_id: str) -> None:
    from agent.graph import run_traced

    print(f"\n{C.BOLD}{C.CYAN}{'='*60}{C.RESET}\n{C.BOLD}ЗАПРОС: {query}{C.RESET}")
    t0 = time.perf_counter()
    try:
        state   = run_traced(query, session_id=session_id, graph=graph)
        elapsed = time.perf_counter() - t0
        print_result(state, elapsed)
    except Exception as e:
        print(f"\n  {_c(C.RED, f'✗ Ошибка: {e}')}")
    print()


# =============================================================
# Режим 3: benchmark (делегируем в benchmark.runner)
# =============================================================

def run_bench(category: str | None = None, verbose: bool = False) -> None:
    from benchmark.runner import run_benchmark
    run_benchmark(category=category, verbose=verbose)


# =============================================================
# Режим 4: health check
# =============================================================

def run_check() -> None:
    from config import settings
    from llm.llm import check_provider
    from observability.tracer import check_langfuse

    print(_c(C.BOLD, "\n╔══════════════════════════════════════════╗"))
    print(_c(C.BOLD,   "║         DB Navigator = health check      ║"))
    print(_c(C.BOLD,   "╚══════════════════════════════════════════╝\n"))

    # LLM
    print(f"  {_c(C.BOLD, 'LLM')}  {settings.active_provider}")
    print(f"    Малая модель  : {settings.model_small}")
    print(f"    Большая модель: {settings.model_large}")
    print(f"  Пингуем...", end=" ", flush=True)
    info = check_provider()
    print(_c(C.GREEN, "✓ OK") if info["ok"] else _c(C.RED, f"✗ {info['error']}"))

    # БД
    print(f"\n  {_c(C.BOLD, 'Серверы БД')}")
    for server in settings.servers:
        dbs = ", ".join(db.name for db in server.databases)
        print(f"    {_c(C.CYAN, server.alias)}  {server.host}:{server.port}  →  {dbs}")
        print(f"    Подключение...", end=" ", flush=True)
        try:
            from db.connector import connector
            connector.execute(server.alias, server.databases[0].name, "SELECT 1 AS ok")
            print(_c(C.GREEN, "✓ OK"))
        except Exception as e:
            print(_c(C.RED, f"✗ {e}"))

    # RAG
    print(f"\n  {_c(C.BOLD, 'RAG индекс')}")
    try:
        from rag.retriever import get_retriever
        r = get_retriever()
        if r.is_ready():
            print(f"    {_c(C.GREEN, '✓')} Индекс готов")
        else:
            print(f"    {_c(C.YELLOW, '⚠')} Пустой = запусти: python -m rag.indexer")
    except Exception as e:
        print(f"    {_c(C.YELLOW, '⚠')} {e}")

    # LangFuse
    print(f"\n  {_c(C.BOLD, 'LangFuse')}")
    lf = check_langfuse()
    if not lf["enabled"]:
        print(f"    {_c(C.DIM, '=')} Выключен: {lf['error']}")
        print(f"    Добавь в .env: LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY")
    else:
        print(f"    Host: {lf['host']}")
        print(f"    Подключение...", end=" ", flush=True)
        print(_c(C.GREEN, "✓ OK") if lf["ok"] else _c(C.RED, f"✗ {lf['error']}"))
    print()


# =============================================================
# Точка входа
# =============================================================

def main() -> None:
    args = sys.argv[1:]

    if not args:
        run_repl()
        return

    if args[0] == "--check":
        run_check()

    elif args[0] == "--bench":
        # python app.py --bench [category] [--verbose]
        category = None
        verbose  = False
        for a in args[1:]:
            if a == "--verbose" or a == "-v":
                verbose = True
            elif not a.startswith("-"):
                category = a
        run_bench(category=category, verbose=verbose)

    elif args[0] in {"--help", "-h"}:
        print(textwrap.dedent("""
            DB Navigator Agent

            python app.py                  = интерактивный REPL
            python app.py "вопрос"         = одиночный запрос
            python app.py --bench          = полный benchmark (12 кейсов)
            python app.py --bench schema   = benchmark одной категории
            python app.py --bench --verbose = с детальным разбором критериев
            python app.py --check          = health check
            python app.py --help           = эта справка

            Benchmark также доступен напрямую:
              python -m benchmark.runner --help
        """))

    else:
        run_single(" ".join(args))


if __name__ == "__main__":
    main()