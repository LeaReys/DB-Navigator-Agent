"""
Агрегирует результаты всех кейсов в сводные метрики.

Метрики:
  - overall_pass_rate       = % кейсов где ВСЕ критерии прошли
  - classification_accuracy = % правильно классифицированных запросов
  - tool_call_accuracy      = % правильных вызовов инструментов
  - sql_safety_rate         = % безопасных SQL (из тех где SQL генерировался)
  - avg_latency_s           = среднее время ответа
  - p90_latency_s           = 90-й перцентиль латентности
  - by_category             = разбивка pass rate по категориям
  - by_criterion            = pass rate по каждому критерию
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean, quantiles
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from benchmark.evaluator import CaseResult


@dataclass
class BenchmarkMetrics:
    total:                int   = 0
    passed:               int   = 0
    failed:               int   = 0
    errors:               int   = 0  # Python-ошибки при запуске, не провалы критериев

    overall_pass_rate:        float = 0.0  # passed / total
    classification_accuracy:  float = 0.0
    tool_call_accuracy:       float = 0.0
    sql_safety_rate:          float = 0.0  # среди кейсов с SQL

    avg_latency_s: float = 0.0
    p90_latency_s: float = 0.0
    total_time_s:  float = 0.0

    by_category:  dict[str, dict] = field(default_factory=dict)
    by_criterion: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "total":                   self.total,
            "passed":                  self.passed,
            "failed":                  self.failed,
            "errors":                  self.errors,
            "overall_pass_rate":       round(self.overall_pass_rate, 4),
            "classification_accuracy": round(self.classification_accuracy, 4),
            "tool_call_accuracy":      round(self.tool_call_accuracy, 4),
            "sql_safety_rate":         round(self.sql_safety_rate, 4),
            "avg_latency_s":           round(self.avg_latency_s, 2),
            "p90_latency_s":           round(self.p90_latency_s, 2),
            "total_time_s":            round(self.total_time_s, 2),
            "by_category":             self.by_category,
            "by_criterion":            self.by_criterion,
        }


def compute(results: list["CaseResult"]) -> BenchmarkMetrics:
    """
    Вычисляет BenchmarkMetrics по списку CaseResult.

    Args:
        results: список результатов от evaluator.evaluate()

    Returns:
        BenchmarkMetrics = все метрики для отчёта и JSON
    """
    if not results:
        return BenchmarkMetrics()

    m = BenchmarkMetrics()
    m.total  = len(results)
    m.passed = sum(1 for r in results if r.passed and not r.error)
    m.failed = sum(1 for r in results if not r.passed and not r.error)
    m.errors = sum(1 for r in results if r.error)

    m.overall_pass_rate = m.passed / m.total

    # = Латентность ================================
    latencies = [r.latency_s for r in results if r.latency_s > 0]
    if latencies:
        m.avg_latency_s = mean(latencies)
        m.total_time_s  = sum(latencies)
        if len(latencies) >= 2:
            m.p90_latency_s = quantiles(latencies, n=10)[8]  # 90th percentile
        else:
            m.p90_latency_s = max(latencies)

    # = Точность по критериям ======================
    criterion_stats: dict[str, list[bool]] = {}
    for result in results:
        if result.error:
            continue
        for cr in result.criteria_results:
            criterion_stats.setdefault(cr.name, []).append(cr.passed)

    for name, values in criterion_stats.items():
        rate = sum(values) / len(values)
        m.by_criterion[name] = {
            "pass_rate": round(rate, 4),
            "passed":    sum(values),
            "total":     len(values),
        }

    m.classification_accuracy = (
        m.by_criterion.get("correct_classification", {}).get("pass_rate", 0.0)
    )
    m.tool_call_accuracy = (
        m.by_criterion.get("correct_tool_call", {}).get("pass_rate", 0.0)
    )
    sql_safety = m.by_criterion.get("sql_readonly", {})
    m.sql_safety_rate = sql_safety.get("pass_rate", 1.0) if sql_safety else 1.0

    # = Разбивка по категориям ====================
    categories: dict[str, list[CaseResult]] = {}
    for r in results:
        categories.setdefault(r.category, []).append(r)

    for cat, cat_results in categories.items():
        cat_passed = sum(1 for r in cat_results if r.passed and not r.error)
        cat_lats   = [r.latency_s for r in cat_results if r.latency_s > 0]
        m.by_category[cat] = {
            "total":       len(cat_results),
            "passed":      cat_passed,
            "pass_rate":   round(cat_passed / len(cat_results), 4),
            "avg_latency": round(mean(cat_lats), 2) if cat_lats else 0.0,
        }

    return m