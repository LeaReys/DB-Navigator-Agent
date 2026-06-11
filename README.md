# DB Navigator Agent

## 1. Концепция агента

**DB Navigator Agent** - AI-агент для backend-разработчика, который помогает быстро ориентироваться в MS SQL Server базах данных: находить нужные таблицы и поля, понимать структуру таблиц, генерировать безопасные read-only T-SQL запросы и получать данные из БД.



## 2. Пользователь

Основной пользователь — backend-разработчик, которому часто нужно быстро понять:
 
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
1. **Роутер 1** — после `classify_intent`: 6 веток по типу запроса
2. **Роутер 2** — после `generate_sql`: выполнять SQL или только вернуть скрипт
3. **Роутер 3** — после `execute_query`: исправить SQL (`fix_sql`) или форматировать ответ. Цикл `fix_sql → execute_query` ограничен счётчиком `sql_retry_count` (до 2 попыток).
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
- не выполняет unsafe SQL — никогда;
- находит релевантные таблицы по бизнес-вопросу;
- генерирует только read-only SQL;
- объясняет ответ на русском языке;
- проходит benchmark из тестовых кейсов с success rate ≥ 80%.

**Метрики (считает `benchmark/metrics.py`, прогон — `python app.py --bench`):**
- `success rate` (overall pass rate) — доля кейсов, где прошли все критерии;
- `latency` — среднее и p90 (см. `p90_latency_s`); по категориям — в разбивке `by_category`;
- `classification_accuracy`, `tool_call_accuracy`, `sql_safety_rate` — точность по типам проверок;
- `cost per run` / расход токенов — доступны в дашборде LangFuse по трейсам прогонов.

Результаты каждого прогона сохраняются в `benchmark/results/run_*.json`.

---
 
## 7. Быстрый старт
 
### Предварительные условия
 
- Python 3.11+
- Docker (для demo-БД)
- ODBC Driver 17 for SQL Server
### 1. Установить зависимости
 
```bash
pip install -r requirements.txt
```
 
### 2. Настроить окружение
 
```bash
cp .env.example .env
# Заполните .env своими значениями:
# - OPENROUTER_API_KEY или OLLAMA_HOST
# - LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY (cloud.langfuse.com)
# - DB_SERVER_1_* (параметры БД)
```
 
### 3. Поднять demo-БД
 
```bash
cd demo
docker-compose up -d
# Подождать ~30 сек пока MS SQL поднимется и init.sql применится
```
 
### 4. Построить RAG-индекс
 
```bash
python -m rag.indexer
```
 
### 5. Проверить что всё работает
 
```bash
python app.py --check
# Должно показать ✓ OK для LLM, БД, RAG и LangFuse
```
 
### 6. Запустить агента
 
```bash
# Интерактивный режим (REPL)
python app.py
 
# Одиночный запрос
python app.py "Какая структура у таблицы debt?"
 
# Прогон benchmark (все 12 кейсов)
python app.py --bench
 
# Только одна категория
python app.py --bench navigation
 
# Справка
python app.py --help
```
 
### 7. Запустить тесты
 
```bash
pip install -r requirements-dev.txt
pytest
```
 
Юнит-тесты в `tests/` покрывают чистую логику без живой инфраструктуры
(БД/LLM/Chroma не нужны): три роутера графа, проверку SQL на мутации,
авто-`TOP`, извлечение ключевых слов, форматирование типов и агрегацию метрик.
 
---
 
## 8. Структура проекта
 
```
db_navigator/
├── agent/
│   ├── graph.py            # LangGraph граф (build_graph, run_traced, 3 роутера)
│   ├── nodes.py            # Логика каждого узла (включая fix_sql retry)
│   └── state.py            # AgentState (TypedDict + Pydantic-модели)
├── observability/
│   ├── __init__.py
│   └── tracer.py           # LangFuse интеграция (трейсинг, метрики)
├── tools/
│   ├── metadata_search.py  # RAG + SQL fallback
│   ├── schema_tool.py      # get_table_schema из sys.columns
│   └── sql_tool.py         # execute_query через pyodbc
├── rag/
│   ├── indexer.py          # Индексация схемы БД в ChromaDB
│   └── retriever.py        # Семантический поиск по индексу
├── llm/
│   ├── llm.py              # Обёртка над OpenRouter / Ollama
│   └── prompts.py          # Шаблоны промптов для всех LLM-узлов
├── db/
│   └── connector.py        # pyodbc-менеджер (пул соединений, read-only guard)
├── schemas/
│   ├── models.py           # Pydantic-модели для всех выходов агента
│   └── sql_safety.py       # Общий паттерн проверки SQL на мутации
├── benchmark/
│   ├── evaluator.py        # Критерии оценки каждого кейса
│   ├── metrics.py          # Агрегация: pass rate, latency, by_category
│   ├── runner.py           # CLI для запуска benchmark
│   └── test_cases.json     # 12 тестовых запросов с критериями
├── demo/
│   ├── docker-compose.yml  # MS SQL 2022 + автоматическая заливка init.sql
│   └── init.sql            # Синтетическая demo-БД (9 таблиц, домен взыскания)
├── config.py               # Все настройки через pydantic-settings + .env
├── app.py                  # Точка входа: REPL / одиночный запрос / benchmark / check
├── requirements.txt        # Зависимости
└── .env.example            # Шаблон переменных окружения
```

---

## 9. Безопасность (в дальнейшем)
 

----

## 10. Возможные расширения проекта 

- [x] подключение OpenRouter/Ollama;
- [x] RAG по схеме БД (по sys-таблицам из MS Server);
- [ ] (?) дополнение RAG по документам описания БД (а не только по sys-таблицам);
- [x] реальный `pyodbc` metadata tool;
- [x] read-only SQL  через SQL Server с ограниченными правами;
- [ ] (?) HITL - уточняющие вопросы у пользователя;
- [x] Retry (SQL self-correction loop: fix_sql → execute_query, до 2 попыток);
- [x] LangFuse;
- [x] benchmark и evals;
- [ ] security-checklist;
- [ ] улучшенная проверка SQL-запроса;
- [ ] Allowlist таблиц;
- [ ] доработка промптов, выбор оптимальных моделей
- [ ] LLM-as-judge 
