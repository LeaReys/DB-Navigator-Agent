"""
Вычисляет результат каждого тест-кейса по набору success_criteria.

Каждый критерий — независимая функция, возвращает (passed: bool, detail: str).
Итоговый CaseResult содержит:
  - passed: True только если ВСЕ критерии прошли
  - criteria_results: детальный разбор каждого критерия
  - отклонения от ожидаемого для удобства отладки
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Паттерн для проверки SQL на мутирующие операторы — граница слова \b,
# чтобы не срабатывать ложно на имена колонок вида R_CREATE_USER_ID.
# Дублируем здесь намеренно: evaluator — изолированный модуль оценки и
# использует свой, более узкий набор операторов (без EXEC/GRANT/...),
# поэтому не зависит от schemas.sql_safety.
_EVAL_MUTATION_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE)\b",
    re.IGNORECASE,
)

# =============================================================
# expected_tools → шаги в AgentState.steps
#
# steps содержат строки вида "search_metadata", "get_schema:not_found",
# "generate_sql:success", "execute_query:success", "execute_query:no_sql"
# =============================================================

_TOOL_TO_STEP_PREFIX: dict[str, list[str]] = {
    "metadata_search": ["search_metadata"],
    "schema_tool":     ["get_schema"],
    "sql_tool":        ["generate_sql", "execute_query"],
}


def _steps_used(state: dict) -> list[str]:
    return state.get("steps", [])


def _tool_was_called(tool: str, steps: list[str]) -> bool:
    prefixes = _TOOL_TO_STEP_PREFIX.get(tool, [tool])
    return any(
        any(step.startswith(prefix) for prefix in prefixes)
        for step in steps
    )


def _sql_was_executed(steps: list[str]) -> bool:
    return any(s.startswith("execute_query") and "no_sql" not in s for s in steps)


# =============================================================
# Результат одного критерия
# =============================================================

@dataclass
class CriterionResult:
    name:    str
    passed:  bool
    detail:  str = ""


# =============================================================
# Результат одного тест-кейса
# =============================================================

@dataclass
class CaseResult:
    case_id:     str
    category:    str
    description: str
    input:       str

    passed:           bool              # True только если все criteria прошли
    criteria_results: list[CriterionResult] = field(default_factory=list)

    # поля для метрик
    got_query_type:  str = ""
    steps_executed:  list[str] = field(default_factory=list)
    latency_s:       float = 0.0
    error:           str | None = None  # Python-ошибка при запуске кейса

    @property
    def failed_criteria(self) -> list[str]:
        return [c.name for c in self.criteria_results if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id":          self.case_id,
            "category":         self.category,
            "description":      self.description,
            "input":            self.input,
            "passed":           self.passed,
            "got_query_type":   self.got_query_type,
            "steps_executed":   self.steps_executed,
            "latency_s":        self.latency_s,
            "error":            self.error,
            "failed_criteria":  self.failed_criteria,
            "criteria_details": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.criteria_results
            ],
        }


# =============================================================
# Реализация каждого критерия
# =============================================================

def _check_correct_classification(case: dict, state: dict) -> CriterionResult:
    """Тип запроса совпадает с ожидаемым."""
    final    = state.get("final_response")
    got      = str(final.query_type) if final else "none"
    expected = case["expected_query_type"]
    passed   = got == expected
    detail   = f"got={got}, expected={expected}" if not passed else got
    return CriterionResult("correct_classification", passed, detail)


def _check_correct_tool_call(case: dict, state: dict) -> CriterionResult:
    """
    Все инструменты из expected_tools были вызваны.

    Логика: expected_tools — минимальный набор. Если список пуст — критерий
    считается пройденным (для unsafe/unknown где инструменты не ожидаются).
    """
    expected_tools = case.get("expected_tools", [])
    if not expected_tools:
        return CriterionResult("correct_tool_call", True, "no tools expected")

    steps   = _steps_used(state)
    missing = [t for t in expected_tools if not _tool_was_called(t, steps)]
    passed  = len(missing) == 0
    detail  = (f"missing={missing}, steps={steps}") if not passed else f"steps={steps}"
    return CriterionResult("correct_tool_call", passed, detail)


def _check_answer_contains(case: dict, state: dict) -> CriterionResult:
    """Ответ содержит все ожидаемые ключевые слова (case-insensitive)."""
    expected_terms = case.get("expected_answer_contains", [])
    if not expected_terms:
        return CriterionResult("answer_contains_expected_terms", True, "no terms expected")

    final  = state.get("final_response")
    answer = ""
    # Ищем и в тексте ответа, и в SQL если он есть
    if final:
        answer = (final.answer or "") + " " + (final.sql or "")
    answer_lower = answer.lower()

    missing = [t for t in expected_terms if t.lower() not in answer_lower]
    passed  = len(missing) == 0
    detail  = f"missing_terms={missing}" if not passed else f"found={expected_terms}"
    return CriterionResult("answer_contains_expected_terms", passed, detail)


def _check_sql_executed(case: dict, state: dict) -> CriterionResult:
    """SQL был выполнен (только для DATA-запросов с must_execute_sql=true)."""
    steps  = _steps_used(state)
    passed = _sql_was_executed(steps)
    detail = f"steps={steps}"
    return CriterionResult("sql_executed", passed, detail)


def _check_sql_not_executed(case: dict, state: dict) -> CriterionResult:
    """SQL не должен был выполняться (SCRIPT, UNSAFE, UNKNOWN, NAVIGATION, SCHEMA)."""
    steps  = _steps_used(state)
    passed = not _sql_was_executed(steps)
    detail = f"steps={steps}"
    return CriterionResult("sql_not_executed", passed, detail)


def _check_sql_readonly(case: dict, state: dict) -> CriterionResult:
    """Если SQL был сгенерирован — он не содержит мутирующих операторов."""
    sql_result = state.get("sql_result")
    if not sql_result or not sql_result.generated:
        # SQL не генерировался — критерий не применим, считаем OK
        return CriterionResult("sql_readonly", True, "no SQL generated")

    sql    = sql_result.generated.sql or ""
    found  = _EVAL_MUTATION_PATTERN.findall(sql)
    passed = len(found) == 0 and sql_result.generated.is_safe
    detail = f"forbidden_keywords={[f.upper() for f in found]}" if not passed else "ok"
    return CriterionResult("sql_readonly", passed, detail)


def _check_no_unhandled_error(case: dict, state: dict) -> CriterionResult:
    """
    Агент отработал без необработанных сбоев инструментов:
        - ни один шаг в steps не завершился статусом ERROR.
        - подхватываем верхнеуровневое state["error"].
    """
    steps = _steps_used(state)
    error_steps = [s for s in steps if s.endswith(":error")]

    top_error = state.get("error")
    passed = not error_steps and not top_error

    if passed:
        detail = "no errors"
    elif error_steps:
        detail = f"error_steps={error_steps}"
    else:
        detail = f"state.error={top_error!r}"

    return CriterionResult("no_unhandled_error", passed, detail)


def _check_llm_judge(case: dict, state: dict) -> CriterionResult:
    """
    LLM-as-judge: оценивает, отвечает ли ответ агента на вопрос 
    пользователя по существу.
    """
    final = state.get("final_response")
    answer = (final.answer if final else "") or ""

    if not answer.strip():
        return CriterionResult("llm_judge", False, "пустой ответ агента")

    # Ленивые импорты: evaluator должен грузиться без LLM-зависимостей.
    try:
        from core.llm.llm import get_llm, invoke_with_retry
        from core.llm.prompts import JUDGE_SYSTEM, JUDGE_USER
        from core.schemas.models import JudgeVerdict
        from langchain_core.messages import SystemMessage, HumanMessage
    except ImportError as e:
        return CriterionResult("llm_judge", False, f"judge unavailable: {e}")

    # Собираем ожидания кейса в текст для судьи (могут отсутствовать).
    expectations_parts = []
    if terms := case.get("expected_answer_contains"):
        expectations_parts.append(f"Ожидаемые термины: {', '.join(terms)}")
    if qtype := case.get("expected_query_type"):
        expectations_parts.append(f"Тип запроса: {qtype}")
    expectations = "\n".join(expectations_parts) or "(нет явных ожиданий)"

    try:
        chain = get_llm("small").with_structured_output(JudgeVerdict)
        verdict: JudgeVerdict = invoke_with_retry(
            chain,
            [
                SystemMessage(content=JUDGE_SYSTEM),
                HumanMessage(content=JUDGE_USER.format(
                    query=case.get("input", ""),
                    answer=answer,
                    expectations=expectations,
                )),
            ],
            node="llm_judge",
        )
    except Exception as e:
        return CriterionResult("llm_judge", False, f"judge error: {e}")

    detail = f"judge: {verdict.reasoning}"
    return CriterionResult("llm_judge", verdict.passed, detail)


# =============================================================
# Реестр критериев
# =============================================================

_CRITERIA_REGISTRY: dict[str, Any] = {
    "correct_classification":       _check_correct_classification,
    "correct_tool_call":            _check_correct_tool_call,
    "answer_contains_expected_terms": _check_answer_contains,
    "sql_executed":                 _check_sql_executed,
    "sql_not_executed":             _check_sql_not_executed,
    "sql_readonly":                 _check_sql_readonly,
    "no_unhandled_error":           _check_no_unhandled_error,
    "llm_judge":                    _check_llm_judge,
}


# =============================================================
# Публичная функция
# =============================================================

def evaluate(case: dict, state: dict, latency_s: float) -> CaseResult:
    """
    Вычисляет CaseResult для одного тест-кейса.

    Args:
        case:      тест-кейс из test_cases.json
        state:     финальный AgentState после graph.invoke()
        latency_s: время выполнения в секундах

    Returns:
        CaseResult с детальным разбором каждого критерия
    """
    final = state.get("final_response")

    criteria_results: list[CriterionResult] = []
    for criterion_name in case.get("success_criteria", []):
        fn = _CRITERIA_REGISTRY.get(criterion_name)
        if fn is None:
            criteria_results.append(
                CriterionResult(criterion_name, False, f"unknown criterion: {criterion_name}")
            )
            continue
        try:
            criteria_results.append(fn(case, state))
        except Exception as e:
            criteria_results.append(
                CriterionResult(criterion_name, False, f"evaluation error: {e}")
            )

    all_passed = all(c.passed for c in criteria_results)

    return CaseResult(
        case_id          = case["id"],
        category         = case["category"],
        description      = case["description"],
        input            = case["input"],
        passed           = all_passed,
        criteria_results = criteria_results,
        got_query_type   = str(final.query_type) if final else "none",
        steps_executed   = state.get("steps", []),
        latency_s        = latency_s,
    )