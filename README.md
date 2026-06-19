# DB Navigator Agent

## 1. Концепция агента

**DB Navigator Agent** - AI-агент для backend-разработчика, который помогает быстро ориентироваться в MS SQL Server базах данных: находить нужные таблицы и поля, понимать структуру таблиц, генерировать безопасные read-only T-SQL запросы и получать данные из БД.



## 2. Пользователь

Основной пользователь - backend-разработчик, которому часто нужно быстро понять:
 
- где хранится нужная бизнес-информация;
- какая структура у таблицы;
- как написать безопасный SELECT-запрос;
- какой статус или значение находится в БД по конкретному идентификатору.

**С какими системами работает агент:**
- MS SQL Server (через pyodbc, read-only соединение)
- ChromaDB (локальный векторный стор для RAG)
- OpenRouter / Ollama (LLM-провайдеры)
- LangFuse (observability, трейсинг, метрики)

---

## Быстрый старт через Docker

Запуск проекта целиком - БД + агент + веб-интерфейс - без локальной установки Python и ODBC.

**Требуется:** Docker + Docker Compose + ключ OpenRouter.

```bash
# 1. Задать ключ OpenRouter
cp .env.docker.example .env
#    впишите OPENROUTER_API_KEY в .env

# 2. Поднять всё
docker compose up --build

# 3. Открыть интерфейс
#    http://localhost:8000
```

Compose поднимает три сервиса:

| Сервис | Назначение |
|--------|-----------|
| `mssql` | MS SQL Server 2022 с demo-базой `db_proglib` |
| `init-db` | разовая заливка `demo/init.sql` - схема, синтетические данные и read-only логин `agent_reader` |
| `app` | FastAPI + LangGraph-агент + чат-интерфейс на `:8000` |

LangFuse подключается опционально: если в `.env` заданы ключи; если нет - агент работает без трейсинга.

---
 
## 3. Архитектура агента

### State
 
Основные поля состояния (`agent/state.py`):
 
| Поле | Тип | Описание |
|------|-----|----------|
| `user_query` | str | Исходный вопрос пользователя |
| `classification` | ClassificationResult | Тип запроса (выход роутера) |
| `metadata_result` | MetadataSearchResult | Найденный контекст по БД (RAG) |
| `schema_result` | TableSchemaResult | Структура конкретной таблицы |
| `sql_result` | SQLGenerationResult | Сгенерированный SQL |
| `execute_result` | ExecuteQueryResult | Результат выполнения SELECT |
| `final_response` | AgentResponse | Итоговый ответ пользователю |
| `error` | str \| None | Текст ошибки агента (производное поле, вычисляется в `format_response` из result-объектов: сбой генерации/выполнения SQL или ненайденная таблица) |
| `steps` | list[str] | История выполненных шагов (для трейсинга) |
 
### Узлы графа
 
| Узел | Описание |
|------|----------|
| `classify_intent` | LLM-классификация типа запроса (малая модель) |
| `search_metadata` | RAG-поиск релевантных таблиц по запросу |
| `get_schema` | Получение структуры таблицы из sys.columns |
| `generate_sql` | Генерация T-SQL через LLM (большая модель) |
| `execute_query` | Выполнение SELECT через pyodbc (только read-only) |
| `fix_sql` | Самоисправление SQL после ошибки выполнения (retry-цикл) |
| `format_response` | Формирование финального ответа (малая модель) |
| `handle_unknown` | Ответ при непонятном запросе |
| `unsafe_query` | Блокировка запросов на изменение данных |
 
### Tools
 
| Tool | Тип | Описание |
|------|-----|----------|
| `metadata_search` | RAG + SQL fallback | Семантический поиск таблиц и полей по запросу |
| `schema_tool` | Внешняя БД | Получение структуры таблицы из системных таблиц MS SQL |
| `sql_tool` | Внешняя БД | Выполнение SELECT-запросов через pyodbc |
 
---
 
## 4. Схема графа
 
```mermaid
flowchart TD
    START([Пользовательский запрос]) --> A[classify_intent<br/>LLM-классификация запроса]
 
    A -->|navigation<br/>Где хранится поле/сущность?| B[search_metadata<br/>RAG-поиск по метаданным]
    A -->|schema<br/>Структура таблицы| C[get_schema<br/>Получение схемы таблицы]
    A -->|script<br/>Нужно написать SQL| D[generate_sql<br/>Генерация SQL]
    A -->|data<br/>Нужно получить данные| D
    A -->|unknown<br/>Непонятный запрос| H[handle_unknown<br/>Ответ с примерами]
    A -->|unsafe<br/>INSERT/UPDATE/DELETE/DROP| U[unsafe_query<br/>Блокировка запроса]
 
    B --> F[format_response<br/>Формирование финального ответа]
    C --> F
 
    D --> R{DATA-запрос<br/>и SQL безопасный?}
 
    R -->|Да| E[execute_query<br/>Выполнение SELECT через pyodbc]
    R -->|Нет| F
 
    E --> G{Ошибка выполнения<br/>и есть попытки?}
    G -->|Да| X[fix_sql<br/>Исправление SQL]
    G -->|Нет| F
    X --> E
 
    F --> END([END])
    H --> END
    U --> END
```
 
**Три ветвления:**
1. **Роутер 1** - после `classify_intent`: 6 веток по типу запроса
2. **Роутер 2** - после `generate_sql`: выполнять SQL или только вернуть скрипт
3. **Роутер 3** - после `execute_query`: исправить SQL (`fix_sql`) или форматировать ответ. Цикл `fix_sql → execute_query` ограничен счётчиком `sql_retry_count` (до 2 попыток).
---

## 5. Edge cases
 
| # | Сценарий | Обработка |
|---|----------|-----------|
| 1 | Пользователь просит UPDATE / DELETE / INSERT | Блокируется классификатором → `unsafe_query` |
| 2 | SQL содержит мутирующий оператор, но классификатор пропустил | Pydantic-валидатор + проверка в коннекторе (defense in depth) |
| 3 | Слишком общий вопрос без бизнес-термина | RAG возвращает топ-результаты, агент отвечает что нашёл |
| 4 | Бизнес-термин отсутствует в RAG-индексе | Fallback на SQL LIKE поиск по sys.tables |
| 5 | Запрос к БД не возвращает строк | `ToolStatus.EMPTY`, агент сообщает что данных нет |
| 6 | Запрошенная таблица явно не указана (SCHEMA-запрос) | `get_schema` вызывает `search_metadata` чтобы найти таблицу |
| 7 | RAG-индекс не построен при первом запуске | Автоматический fallback на SQL LIKE, агент не падает |
| 8 | LLM-провайдер недоступен | `try/except` в каждом узле, возврат `QueryType.UNKNOWN` |
 
---
 
## 6. Критерии качества
 
Агент работает хорошо, если:
 
- правильно классифицирует тип запроса;
- не выполняет unsafe SQL - никогда;
- находит релевантные таблицы по бизнес-вопросу;
- генерирует только read-only SQL;
- объясняет ответ на русском языке;
- проходит benchmark из тестовых кейсов с success rate ≥ 80%.

**Метрики (считает `benchmark/metrics.py`, прогон - `python app.py --bench`):**
- `success rate` (overall pass rate) - доля кейсов, где прошли все критерии;
- `latency` - среднее и p90 (см. `p90_latency_s`); по категориям - в разбивке `by_category`;
- `classification_accuracy`, `tool_call_accuracy`, `sql_safety_rate` - точность по типам проверок;
- `no_unhandled_error` - детерминированный ассерт: ни один шаг агента не завершился сбоем (`:error`), при этом штатные исходы (`:empty`, блокировка unsafe, таблица не найдена) ошибкой не считаются;
- `cost per run` / расход токенов - доступны в дашборде LangFuse по трейсам прогонов.

Результаты каждого прогона сохраняются в `benchmark/results/run_*.json`.

---
 
### 6. Запустить unit-тесты
 
```bash
pip install -r requirements-dev.txt
pytest
```
 
Юнит-тесты в `tests/` покрывают чистую логику без живой инфраструктуры
(БД/LLM/Chroma не нужны): три роутера графа, проверку SQL на мутации,
авто-`TOP`, извлечение ключевых слов, форматирование типов и агрегацию метрик.
 
---
 
## Метрики последнего прогона

### Общие метрики

| Метрика | Значение |
|---------|----------|
| **Всего кейсов** | 12 |
| **Успешных (pass)** | 11 |
| **Провалено** | 1 |
| **Ошибок выполнения** | 0 |
| **Success Rate** | 91.7% |
| **Avg Latency** | 0.8 s |
| **P90 Latency** | 1.4 s |
| **Всего времени** | 9.6 s |

### По критериям (качество)

| Критерий | Accuracy | Кейсов |
|----------|----------|--------|
| Правильная классификация | 100% | 12/12 |
| Правильные инструменты | 91.7% | 11/12 |
| Безопасность SQL | 100% | 12/12 |
| Ожидаемые термины в ответе | 91.7% | 11/12 |

### По категориям

| Категория | Pass Rate | Кейсов | Avg Latency |
|-----------|-----------|--------|-------------|
| navigation | 100% | 3/3 | 0.5 s |
| schema | 100% | 2/2 | 0.4 s |
| script | 100% | 2/2 | 1.1 s |
| data | 83% | 2/3 | 1.1 s |
| unsafe | 100% | 2/2 | 0.3 s |
| unknown | 100% | 1/1 | 0.2 s |

### Как получить метрики

```bash
# Запустить benchmark и сохранить результаты
python app.py --bench

# Результаты сохраняются в benchmark/results/run_YYYYMMDD_HHMMSS.json
```

### Observability

 Трейсы каждого прогона доступны в **LangFuse** (если ключи заданы в .env).

---
 
## 8. Структура проекта
 
```
db_navigator/
├── api/
│   ├── server.py                # FastAPI: SSE-чат, /api/health, отдача статики
│   ├── events.py                # Перевод обновлений узлов графа в события для UI
│   └── static/                  # Чат-интерфейс (index.html / style.css / app.js)
├── benchmark/
│   ├── evaluator.py             # Критерии оценки каждого кейса
│   ├── metrics.py               # Агрегация: pass rate, latency, by_category
│   ├── runner.py                # CLI для запуска benchmark
│   └── test_cases.json          # 17 тестовых запросов с критериями
├── core/
│   ├── agent/
│   │   ├── graph.py             # LangGraph граф (build_graph, run_traced, 3 роутера)
│   │   ├── nodes.py             # Логика каждого узла (включая fix_sql retry)
│   │   └── state.py             # AgentState (TypedDict + Pydantic-модели)
│   ├── observability/
│   │   ├── __init__.py
│   │   └── tracer.py            # LangFuse интеграция (трейсинг, метрики)
│   ├── tools/
│   │   ├── metadata_search.py   # RAG + SQL fallback
│   │   ├── schema_tool.py       # get_table_schema из sys.columns
│   │   └── sql_tool.py          # execute_query через pyodbc
│   ├── rag/
│   │   ├── indexer.py           # Индексация схемы БД в ChromaDB
│   │   └── retriever.py         # Семантический поиск по индексу
│   ├── llm/
│   │   ├── llm.py               # Обёртка над OpenRouter / Ollama
│   │   └── prompts.py           # Шаблоны промптов для всех LLM-узлов
│   ├── db/
│   │   └── connector.py         # pyodbc-менеджер (пул соединений, read-only guard)
│   ├── schemas/
│   │   ├── models.py            # Pydantic-модели для всех выходов агента
│   │   └── sql_safety.py        # Общий паттерн проверки SQL на мутации
│   ├── config.py                # Все настройки через pydantic-settings + .env
├── demo/
│   ├── docker-compose.yml       # Только БД: MS SQL 2022 + заливка init.sql
│   └── init.sql                 # Синтетическая demo-БД (9 таблиц, домен взыскания)
├── docker-compose.yml           # Весь проект: mssql + init-db + app (веб-агент)
├── Dockerfile                   # Образ агента: Python + ODBC Driver 17 + модель эмбеддингов
├── .dockerignore                # chroma_db оставляем, .env и кеши исключаем
├── .env.docker.example          # Шаблон .env для docker compose (нужен OPENROUTER_API_KEY)
├── app.py                       # Точка входа CLI: REPL / запрос / benchmark / check
├── requirements.txt             # Зависимости агента
├── requirements-web.txt         # Зависимости веб-слоя (FastAPI + uvicorn)
└── .env.example                 # Шаблон переменных окружения
```

---

## 9. Безопасность (в дальнейшем)
 

----

## 10. Возможные расширения проекта 

- [x] подключение OpenRouter/Ollama;
- [x] RAG по схеме БД (по sys-таблицам из MS Server);
- [x] (?) дополнение RAG по документам описания БД (а не только по sys-таблицам);
- [x] реальный `pyodbc` metadata tool;
- [x] read-only SQL  через SQL Server с ограниченными правами;
- [ ] (?) HITL - уточняющие вопросы у пользователя;
- [x] Retry (SQL self-correction loop: fix_sql → execute_query, до 2 попыток);
- [x] LangFuse;
- [x] benchmark и evals;
- [ ] security-checklist;
- [x] улучшенная проверка SQL-запроса;
- [ ] Allowlist таблиц и БД;
- [x] доработка промптов, выбор оптимальных моделей
- [ ] LLM-as-judge
- [x] Обработка вывода исключений (поле `error` + показ пользователю в финальном ответе).
- [ ] Продолжение диалога(сохранение контекста)
