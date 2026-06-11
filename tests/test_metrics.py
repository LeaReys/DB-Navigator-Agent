"""
Тесты для benchmark/metrics.py::compute — агрегация результатов прогона.
evaluator и metrics — чистый stdlib (dataclasses + statistics), без БД и LLM.
"""

from benchmark.evaluator import CaseResult, CriterionResult
from benchmark.metrics import compute


def _case(passed=True, error=None, latency=1.0, category="data", criteria=None):
    return CaseResult(
        case_id="c",
        category=category,
        description="d",
        input="i",
        passed=passed,
        criteria_results=criteria or [],
        latency_s=latency,
        error=error,
    )


def test_empty_results():
    m = compute([])
    assert m.total == 0
    assert m.overall_pass_rate == 0.0


def test_all_pass():
    results = [
        _case(passed=True, criteria=[CriterionResult("correct_classification", True)])
        for _ in range(4)
    ]
    m = compute(results)
    assert m.total == 4
    assert m.passed == 4
    assert m.overall_pass_rate == 1.0
    assert m.classification_accuracy == 1.0


def test_mixed_pass_fail_error():
    results = [
        _case(passed=True, criteria=[CriterionResult("correct_classification", True)]),
        _case(passed=False, criteria=[CriterionResult("correct_classification", False)]),
        _case(passed=False, error="boom"),
    ]
    m = compute(results)
    assert m.total == 3
    assert m.passed == 1
    assert m.failed == 1
    assert m.errors == 1
    # точность считается только по не-ошибочным кейсам: 1 из 2
    assert m.classification_accuracy == 0.5


def test_latency_avg_and_p90():
    results = [_case(latency=float(i)) for i in range(1, 11)]
    m = compute(results)
    assert m.avg_latency_s == 5.5
    assert m.p90_latency_s >= m.avg_latency_s


def test_sql_safety_rate_defaults_to_one_when_no_sql_criterion():
    results = [_case(passed=True, criteria=[CriterionResult("correct_classification", True)])]
    m = compute(results)
    assert m.sql_safety_rate == 1.0


def test_by_category_breakdown():
    results = [
        _case(category="data", passed=True),
        _case(category="schema", passed=False),
    ]
    m = compute(results)
    assert m.by_category["data"]["pass_rate"] == 1.0
    assert m.by_category["schema"]["pass_rate"] == 0.0
