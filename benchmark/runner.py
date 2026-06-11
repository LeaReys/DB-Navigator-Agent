"""
Точка входа для eval-прогона.

Запуск:
  python -m benchmark.runner                     # все кейсы
  python -m benchmark.runner --category schema   # только одна категория
  python -m benchmark.runner --id nav_01         # один кейс
  python -m benchmark.runner --no-langfuse       # без трассировки
  python -m benchmark.runner --verbose           # детальный вывод критериев

Результаты сохраняются в benchmark/results/run_YYYYMMDD_HHMMSS.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

# =============================================================
# ANSI-цвета
# =============================================================

class C:
    RESET  = "\033[0m";  BOLD   = "\033[1m";  DIM    = "\033[2m"
    CYAN   = "\033[36m"; GREEN  = "\033[32m";  YELLOW = "\033[33m"
    RED    = "\033[31m"; BLUE   = "\033[34m";  WHITE  = "\033[37m"

def _c(color, text): return f"{color}{text}{C.RESET}"


# =============================================================
# Загрузка кейсов
# =============================================================

CASES_PATH = Path(__file__).parent / "test_cases.json"


def load_cases(
    category: str | None = None,
    case_id:  str | None = None,
) -> list[dict]:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if category:
        cases = [c for c in cases if c["category"] == category]
    if case_id:
        cases = [c for c in cases if c["id"] == case_id]
    if not cases:
        raise ValueError(f"Кейсы не найдены (category={category}, id={case_id})")
    return cases


# =============================================================
# Вывод
# =============================================================

_CATEGORY_COLORS = {
    "navigation": C.CYAN,
    "schema":     C.BLUE,
    "script":     C.YELLOW,
    "data":       C.GREEN,
    "unsafe":     C.RED,
    "unknown":    C.DIM,
}

def _cat(category: str) -> str:
    color = _CATEGORY_COLORS.get(category, C.WHITE)
    return _c(color, f"[{category:<10}]")


def print_case_result(result, verbose: bool = False) -> None:
    from benchmark.evaluator import CaseResult

    icon   = _c(C.GREEN, "✓") if result.passed else _c(C.RED, "✗")
    status = _c(C.GREEN, "PASS") if result.passed else _c(C.RED, "FAIL")
    if result.error:
        icon   = _c(C.YELLOW, "!")
        status = _c(C.YELLOW, "ERROR")

    cat_label  = _cat(result.category)
    type_color = C.GREEN if result.got_query_type == "none" else C.WHITE
    type_str   = _c(C.DIM, result.got_query_type)

    print(
        f"  {icon} {cat_label} "
        f"{_c(C.BOLD, result.case_id):<20} "
        f"{status}  {type_str}  "
        f"{_c(C.DIM, f'{result.latency_s:.1f}s')}"
    )

    if result.error:
        print(f"       {_c(C.YELLOW, result.error)}")
        return

    if not result.passed and result.failed_criteria:
        print(f"       Провалено: {_c(C.RED, ', '.join(result.failed_criteria))}")

    if verbose:
        for cr in result.criteria_results:
            mark   = _c(C.GREEN, "✓") if cr.passed else _c(C.RED, "✗")
            detail = f"  {_c(C.DIM, cr.detail)}" if cr.detail else ""
            print(f"         {mark} {cr.name}{detail}")


def print_metrics(metrics) -> None:
    def _rate(v: float) -> str:
        color = C.GREEN if v >= 0.8 else C.YELLOW if v >= 0.6 else C.RED
        return _c(color, f"{v*100:.0f}%")

    print(f"\n  {_c(C.BOLD, 'Метрики')}")
    print(f"  {'=' * 50}")
    print(f"  Прошло / Упало / Ошибок : "
          f"{_c(C.GREEN, str(metrics.passed))} / "
          f"{_c(C.RED, str(metrics.failed))} / "
          f"{_c(C.YELLOW, str(metrics.errors))} "
          f"из {metrics.total}")
    print(f"  {'=' * 50}")
    print(f"  Overall pass rate       : {_rate(metrics.overall_pass_rate)}")
    print(f"  Classification accuracy : {_rate(metrics.classification_accuracy)}")
    print(f"  Tool call accuracy      : {_rate(metrics.tool_call_accuracy)}")
    print(f"  SQL safety rate         : {_rate(metrics.sql_safety_rate)}")
    print(f"  {'=' * 50}")
    print(f"  Avg latency             : {_c(C.DIM, f'{metrics.avg_latency_s:.1f}s')}")
    print(f"  P90 latency             : {_c(C.DIM, f'{metrics.p90_latency_s:.1f}s')}")
    print(f"  Total time              : {_c(C.DIM, f'{metrics.total_time_s:.1f}s')}")

    if metrics.by_category:
        print(f"\n  {_c(C.BOLD, 'По категориям')}")
        print(f"  {'=' * 50}")
        for cat, stat in sorted(metrics.by_category.items()):
            bar    = "█" * int(stat["pass_rate"] * 10) + "░" * (10 - int(stat["pass_rate"] * 10))
            color  = C.GREEN if stat["pass_rate"] >= 0.8 else C.YELLOW if stat["pass_rate"] >= 0.5 else C.RED
            label  = _cat(cat)
            pct     = f"{stat['pass_rate']*100:.0f}%"
            avg_lat = f"{stat['avg_latency']}s avg"
            print(
                f"  {label}  "
                f"{_c(color, bar)}  "
                f"{_c(color, pct)}  "
                f"({stat['passed']}/{stat['total']})  "
                f"{_c(C.DIM, avg_lat)}"
            )

    if metrics.by_criterion:
        print(f"\n  {_c(C.BOLD, 'По критериям')}")
        print(f"  {'=' * 50}")
        for name, stat in sorted(metrics.by_criterion.items()):
            mark  = _c(C.GREEN, "✓") if stat["pass_rate"] >= 0.8 else _c(C.RED, "✗")
            rate    = _rate(stat["pass_rate"])
            counts  = f"({stat['passed']}/{stat['total']})"
            print(f"  {mark} {name:<40} {rate}  {_c(C.DIM, counts)}")


# =============================================================
# Сохранение результатов
# =============================================================

def save_results(results: list, metrics, run_meta: dict) -> Path:
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = results_dir / f"run_{ts}.json"

    payload = {
        "run_meta":    run_meta,
        "metrics":     metrics.to_dict(),
        "case_results": [r.to_dict() for r in results],
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


# =============================================================
# Главная функция
# =============================================================

def run_benchmark(
    category:     str | None = None,
    case_id:      str | None = None,
    use_langfuse: bool = True,
    verbose:      bool = False,
) -> tuple[list, object]:
    """
    Запускает benchmark и возвращает (results, metrics).
    Может использоваться программно из app.py или тестов.
    """
    from agent.graph import build_graph, run_traced
    from agent.state import AgentState
    from benchmark.evaluator import evaluate, CaseResult
    from benchmark import metrics as metrics_module
    from config import settings
    from observability.tracer import is_enabled as lf_enabled

    cases      = load_cases(category=category, case_id=case_id)
    graph      = build_graph()
    session_id = f"bench_{uuid.uuid4().hex[:8]}"

    # = Шапка ====================================
    print(_c(C.BOLD, "\n╔══════════════════════════════════════════════════╗"))
    print(_c(C.BOLD,   "║         DB Navigator = Benchmark Runner          ║"))
    print(_c(C.BOLD,   "╚══════════════════════════════════════════════════╝"))
    print(f"  Провайдер  : {_c(C.GREEN, settings.active_provider)}"
          f"  |  Модели: {_c(C.DIM, settings.model_small)} / {_c(C.DIM, settings.model_large)}")
    lf_str = (
        _c(C.GREEN, f"✓ session={session_id}")
        if (use_langfuse and lf_enabled())
        else _c(C.DIM, "выключен")
    )
    print(f"  LangFuse   : {lf_str}")
    print(f"  Кейсов     : {len(cases)}")
    print(f"  {'=' * 50}\n")

    results: list[CaseResult] = []

    for i, case in enumerate(cases, 1):
        prefix = f"  {_c(C.DIM, f'[{i:02d}/{len(cases)}]')}"
        print(f"{prefix} {_c(C.DIM, case['description'])}")

        # == Запускаем агента ==============================
        t0    = time.perf_counter()
        error = None
        state: dict = {}

        try:
            if use_langfuse and lf_enabled():
                state = run_traced(case["input"], session_id=session_id, graph=graph)
            else:
                state = graph.invoke({"user_query": case["input"], "steps": []})
        except Exception as e:
            error = str(e)

        elapsed = time.perf_counter() - t0

        # == Оцениваем результат ===========================
        if error:
            result = CaseResult(
                case_id    = case["id"],
                category   = case["category"],
                description= case["description"],
                input      = case["input"],
                passed     = False,
                latency_s  = elapsed,
                error      = error,
            )
        else:
            result = evaluate(case, state, elapsed)

        results.append(result)
        print_case_result(result, verbose=verbose)

    # = Метрики ====================================
    m = metrics_module.compute(results)
    print_metrics(m)

    # = Сохранение ================================
    from config import settings as cfg
    run_meta = {
        "timestamp":    datetime.now().isoformat(),
        "session_id":   session_id,
        "provider":     cfg.active_provider,
        "model_small":  cfg.model_small,
        "model_large":  cfg.model_large,
        "langfuse":     use_langfuse and lf_enabled(),
        "total_cases":  len(cases),
    }
    out_path = save_results(results, m, run_meta)
    print(f"\n  Результаты сохранены: {_c(C.DIM, str(out_path))}\n")

    return results, m


# =============================================================
# CLI
# =============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="DB Navigator benchmark runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python -m benchmark.runner
  python -m benchmark.runner --category schema
  python -m benchmark.runner --id nav_01 --verbose
  python -m benchmark.runner --no-langfuse
        """,
    )
    parser.add_argument(
        "--category", "-c",
        choices=["navigation", "schema", "script", "data", "unsafe", "unknown"],
        help="Запустить только кейсы одной категории",
    )
    parser.add_argument(
        "--id",
        metavar="CASE_ID",
        help="Запустить один кейс по id (например: nav_01)",
    )
    parser.add_argument(
        "--no-langfuse",
        action="store_true",
        help="Не писать traces в LangFuse",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Показывать детальный результат каждого критерия",
    )

    args = parser.parse_args()

    try:
        run_benchmark(
            category     = args.category,
            case_id      = args.id,
            use_langfuse = not args.no_langfuse,
            verbose      = args.verbose,
        )
    except ValueError as e:
        print(f"\n  {_c(C.RED, f'Ошибка: {e}')}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()