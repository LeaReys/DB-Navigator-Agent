"""
Точка входа DB Navigator Agent.

Три режима запуска:
  python app.py               → интерактивный REPL (диалог)
  python app.py "вопрос"      → одиночный запрос
  python app.py --test        → прогон набора тестовых запросов
  python app.py --check       → проверка подключения к LLM и БД
"""

from __future__ import annotations

import sys
import time
import logging
import textwrap
from typing import Any

# =============================================================
# Настройка логирования — до любых других импортов
# =============================================================

logging.basicConfig(
    level  = logging.WARNING,          # WARNING+ в консоль
    format = "%(levelname)s: %(message)s",
)
# Подробные логи самого агента — только INFO
logging.getLogger("agent").setLevel(logging.INFO)
logging.getLogger("llm").setLevel(logging.INFO)
logging.getLogger("tools").setLevel(logging.INFO)

# =============================================================
# ANSI-цвета (работают в большинстве терминалов)
# =============================================================

class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    CYAN   = "\033[36m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    RED    = "\033[31m"
    BLUE   = "\033[34m"
    MAGENTA= "\033[35m"
    WHITE  = "\033[37m"

def _c(color: str, text: str) -> str:
    return f"{color}{text}{C.RESET}"

def _header(text: str) -> str:
    return f"\n{C.BOLD}{C.CYAN}{'=' * 60}{C.RESET}\n{C.BOLD}{text}{C.RESET}"

def _section(label: str) -> str:
    return f"\n{C.BOLD}{C.YELLOW}▶ {label}{C.RESET}"


# =============================================================
# Форматирование результата
# =============================================================

_TYPE_ICONS = {
    "navigation": "🔍",
    "schema":     "📋",
    "script":     "📝",
    "data":       "📊",
    "unsafe":     "🚫",
    "unknown":    "❓",
}

def print_result(state: dict, elapsed: float) -> None:
    """Выводит финальный стейт агента в читаемом виде."""
    final = state.get("final_response")
    if not final:
        print(_c(C.RED, "  [ошибка] final_response отсутствует в стейте"))
        return

    query_type = str(final.query_type)
    icon       = _TYPE_ICONS.get(query_type, "•")
    conf_color = C.GREEN if final.confidence >= 0.7 else C.YELLOW if final.confidence >= 0.4 else C.RED

    # Тип + уверенность
    print(f"\n  {icon}  Тип: {_c(C.BOLD, query_type)}"
          f"  |  Уверенность: {_c(conf_color, f'{final.confidence:.0%}')}"
          f"  |  Время: {_c(C.DIM, f'{elapsed:.1f}s')}")

    # Шаги агента
    steps = state.get("steps", [])
    if steps:
        print(f"  {_c(C.DIM, '  →  '.join(steps))}")

    # Основной ответ
    print(_section("Ответ"))
    for line in final.answer.splitlines():
        print(f"  {line}")

    # SQL-скрипт (если есть)
    if final.sql:
        print(_section("SQL"))
        for line in final.sql.splitlines():
            print(f"  {_c(C.CYAN, line)}")

    # Источники
    if final.sources:
        print(_section("Источники"))
        seen: set[str] = set()
        for src in final.sources:
            key = f"{src.server}/{src.database}/{src.table or ''}"
            if key not in seen:
                seen.add(key)
                table_part = f".{_c(C.BOLD, src.table)}" if src.table else ""
                print(f"  {_c(C.DIM, src.server)}  /  {src.database}{table_part}")


def print_error(e: Exception) -> None:
    print(f"\n  {_c(C.RED, '✗ Ошибка:')} {e}")


# =============================================================
# Режим 1: интерактивный REPL
# =============================================================

def run_repl() -> None:
    from agent.graph import build_graph
    from config import settings

    graph = build_graph()

    print(_c(C.BOLD, "\n╔══════════════════════════════════════════╗"))
    print(_c(C.BOLD,   "║     DB Navigator — интерактивный режим   ║"))
    print(_c(C.BOLD,   "╚══════════════════════════════════════════╝"))
    print(f"  Провайдер: {_c(C.GREEN, settings.active_provider)}"
          f"  |  Малая модель: {_c(C.DIM, settings.model_small)}"
          f"  |  Большая: {_c(C.DIM, settings.model_large)}")
    print(f"  Команды: {_c(C.DIM, 'exit / quit / Ctrl+C')} — выход\n")

    while True:
        try:
            query = input(f"{_c(C.BOLD, _c(C.CYAN, '▶ Вопрос:'))} ").strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{_c(C.DIM, 'Выход.')}")
            break

        if not query:
            continue
        if query.lower() in {"exit", "quit", "q"}:
            print(_c(C.DIM, "Выход."))
            break

        _run_single(graph, query)


# =============================================================
# Режим 2: одиночный запрос
# =============================================================

def run_single(query: str) -> None:
    from agent.graph import build_graph
    graph = build_graph()
    _run_single(graph, query)


def _run_single(graph: Any, query: str) -> None:
    """Выполняет один запрос и печатает результат."""
    print(_header(f"ЗАПРОС: {query}"))

    t0 = time.perf_counter()
    try:
        state = graph.invoke({"user_query": query, "steps": []})
        elapsed = time.perf_counter() - t0
        print_result(state, elapsed)
    except Exception as e:
        print_error(e)

    print()


# =============================================================
# Режим 3: тестовый прогон
# =============================================================

# Тестовые запросы: (описание, запрос, ожидаемый тип)
TEST_CASES: list[tuple[str, str, str]] = [
    ("Навигация: поиск статуса",        "Где хранится статус должника?",              "navigation"),
    ("Навигация: поиск суммы долга",    "В какой таблице хранится сумма долга?",      "navigation"),
    ("Схема: конкретная таблица",       "Какая структура таблицы debt?",              "schema"),
    ("Схема: с опечаткой",             "Покажи колонки таблицы doc_links",            "schema"),
    ("Скрипт: агрегация",              "Напиши скрипт для последнего загруженного файла",    "script"),
    ("Скрипт: фильтрация",             "SQL запрос для получения активных долгов",    "script"),
    ("Данные: конкретный id",          "Какие паспортные данные у должника с id 2701002?",   "data"),
    ("Данные: последние записи",       "Покажи id последние 10 загруженных долгов",   "data"),
    ("Небезопасный: удаление",         "Удали все записи из таблицы doc_links",       "unsafe"),
    ("Небезопасный: изменение",        "Измени ФИО должника 2701003",                 "unsafe"),
    ("Неизвестный: офтопик",           "Какая погода в Москве?",                      "unknown"),
    ("Неизвестный: пустой",            "помоги",                                      "unknown"),
]

def run_test() -> None:
    from agent.graph import build_graph
    from config import settings

    graph = build_graph()

    print(_c(C.BOLD, "\n╔══════════════════════════════════════════╗"))
    print(_c(C.BOLD,   "║         DB Navigator — тестовый прогон   ║"))
    print(_c(C.BOLD,   "╚══════════════════════════════════════════╝"))
    print(f"  Провайдер: {_c(C.GREEN, settings.active_provider)}"
          f"  |  Тестов: {len(TEST_CASES)}\n")

    passed = 0
    failed = 0
    errors = 0
    total_time = 0.0

    for i, (description, query, expected_type) in enumerate(TEST_CASES, 1):
        print(f"  {_c(C.DIM, f'[{i:02d}/{len(TEST_CASES)}]')} {description}")
        print(f"         {_c(C.DIM, repr(query))}")

        t0 = time.perf_counter()
        try:
            state   = graph.invoke({"user_query": query, "steps": []})
            elapsed = time.perf_counter() - t0
            total_time += elapsed

            final     = state.get("final_response")
            got_type  = str(final.query_type) if final else "none"
            ok        = got_type == expected_type

            status_icon  = _c(C.GREEN, "✓ PASS") if ok else _c(C.RED, "✗ FAIL")
            type_display = (
                _c(C.GREEN, got_type) if ok
                else f"{_c(C.RED, got_type)} (ожидалось: {expected_type})"
            )

            print(f"         {status_icon}  тип={type_display}"
                  f"  {_c(C.DIM, f'{elapsed:.1f}s')}")

            if ok:
                passed += 1
            else:
                failed += 1

        except Exception as e:
            elapsed = time.perf_counter() - t0
            total_time += elapsed
            print(f"         {_c(C.RED, '✗ ERROR')}  {e}")
            errors += 1

        print()

    # Итоги
    total    = len(TEST_CASES)
    success_rate = passed / total * 100
    rate_color   = C.GREEN if success_rate >= 80 else C.YELLOW if success_rate >= 60 else C.RED

    print("=" * 60)
    print(f"  Результат:  "
          f"{_c(C.GREEN, f'{passed} прошло')}  "
          f"{_c(C.RED, f'{failed} упало')}  "
          f"{_c(C.YELLOW, f'{errors} ошибок')}")
    print(f"  Success rate:  {_c(rate_color, f'{success_rate:.0f}%')}")
    print(f"  Общее время:   {total_time:.1f}s  "
          f"({_c(C.DIM, f'avg {total_time/total:.1f}s/запрос')})")
    print()


# =============================================================
# Режим 4: health check
# =============================================================

def run_check() -> None:
    from config import settings
    from llm.llm import check_provider

    print(_c(C.BOLD, "\n╔══════════════════════════════════════════╗"))
    print(_c(C.BOLD,   "║         DB Navigator — health check      ║"))
    print(_c(C.BOLD,   "╚══════════════════════════════════════════╝\n"))

    # LLM
    print(f"  {_c(C.BOLD, 'LLM провайдер:')} {settings.active_provider}")
    print(f"    Малая модель : {settings.model_small}")
    print(f"    Большая модель: {settings.model_large}")
    print(f"  Пингуем провайдер...", end=" ", flush=True)

    info = check_provider()
    if info["ok"]:
        print(_c(C.GREEN, "✓ OK"))
    else:
        print(_c(C.RED, f"✗ ОШИБКА: {info['error']}"))

    # БД
    print(f"\n  {_c(C.BOLD, 'Серверы БД:')}")
    for server in settings.servers:
        dbs = ", ".join(db.name for db in server.databases)
        print(f"    {_c(C.CYAN, server.alias)}  {server.host}:{server.port}  →  {dbs}")

        # Пробуем подключиться
        print(f"      Подключение...", end=" ", flush=True)
        try:
            from db.connector import connector
            connector.execute(server.alias, server.databases[0].name, "SELECT 1 AS ok")
            print(_c(C.GREEN, "✓ OK"))
        except Exception as e:
            print(_c(C.RED, f"✗ {e}"))

    # RAG
    print(f"\n  {_c(C.BOLD, 'RAG индекс:')}")
    try:
        from rag.retriever import get_retriever
        r = get_retriever()
        if r.is_ready():
            print(f"    {_c(C.GREEN, '✓')} Индекс готов")
        else:
            print(f"    {_c(C.YELLOW, '⚠')} Индекс пустой — запусти: python -m rag.indexer")
    except Exception as e:
        print(f"    {_c(C.YELLOW, '⚠')} {e}")

    print()


# =============================================================
# Точка входа
# =============================================================

def main() -> None:
    args = sys.argv[1:]

    if not args:
        run_repl()
    elif args[0] == "--test":
        run_test()
    elif args[0] == "--check":
        run_check()
    elif args[0] in {"--help", "-h"}:
        print(textwrap.dedent("""
            DB Navigator Agent

            Использование:
              python app.py               — интерактивный режим (REPL)
              python app.py "вопрос"      — одиночный запрос
              python app.py --test        — прогон тестовых запросов
              python app.py --check       — проверка подключений
              python app.py --help        — эта справка
        """))
    else:
        # Всё остальное — одиночный запрос
        run_single(" ".join(args))


if __name__ == "__main__":
    main()