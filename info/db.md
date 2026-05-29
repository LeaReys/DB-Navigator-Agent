Теперь разберём каждый шаг детально.

---

## Шаг ①  — `_check_query_safety(sql)`

Это первое, что происходит при любом вызове `execute()`, ещё до обращения к пулу. Функция берёт SQL-строку, приводит её к верхнему регистру и ищет запрещённые слова:

```python
FORBIDDEN_KEYWORDS = frozenset({
    "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE",
    "ALTER", "CREATE", "EXEC", "EXECUTE", "GRANT", "REVOKE",
})
```

Почему это **второй** рубеж, а не первый? Потому что первый — валидатор в Pydantic-модели `GeneratedSQL`. Коннектор не знает откуда пришёл SQL — от LLM, из теста или из кода. Поэтому он проверяет всегда, независимо от источника.

---

## Шаги ② и ③ — пул подключений `_pool`

`_pool` — это обычный словарь Python. Ключ — кортеж `("server_alias", "database_name")`, значение — живой объект `pyodbc.Connection`.

```python
# Что происходит при первом вызове:
connector.execute("prod", "BankingDB", ...)

_pool = {}                               # пусто
# → ключа нет → идём создавать новое соединение

# При втором вызове:
_pool = {("prod", "BankingDB"): <conn>}  # уже есть
# → проверяем живое ли → отдаём
```

Почему это важно? Открытие нового ODBC-соединения — дорогая операция: сетевой handshake, аутентификация, выделение ресурсов на стороне SQL Server. Это занимает 200–500ms. Переиспользование готового соединения — единицы миллисекунд.

---

## Шаг ④ — ping перед использованием

Соединение могло умереть пока агент бездействовал: SQL Server перезапустился, сеть мигнула, сработал таймаут. Поэтому перед каждым использованием делаем ping:

```python
try:
    self._pool[key].execute("SELECT 1")   # ← ping
    return self._pool[key]                # жив — возвращаем
except pyodbc.Error:
    del self._pool[key]                   # умер — удаляем
    # дальше → шаг ⑤, создаём новое
```

Без этого: первый запрос после долгого простоя падал бы с непонятной ошибкой `"Connection is closed"`.

---

## Шаг ⑤ — создание нового соединения

`config.py` умеет собрать строку подключения в зависимости от того, настроена ли Windows-аутентификация или SQL-логин:

```python
# Windows Auth (если username пустой):
"DRIVER={ODBC Driver 17};SERVER=host,1433;DATABASE=db;Trusted_Connection=yes;"

# SQL Auth:
"DRIVER={ODBC Driver 17};SERVER=host,1433;DATABASE=db;UID=sa;PWD=secret;"
```

После создания сразу ставим `autocommit=True`. Без этого pyodbc неявно открывает транзакцию при первом запросе — и она висит открытой. Это блокирует ресурсы на SQL Server и может привести к дедлокам.

---

## Шаг ⑥ — `cursor.execute(sql, params)`

Параметры передаются **отдельно** от SQL-строки, через `?`-плейсхолдеры:

```python
# Правильно:
cursor.execute("SELECT * FROM debt WHERE id = ?", (123,))

# Неправильно (уязвимость SQL-инъекции):
cursor.execute(f"SELECT * FROM debt WHERE id = {user_input}")
```

pyodbc при параметрическом вызове передаёт значения через отдельный протокол TDS — SQL Server видит их как данные, а не как часть запроса. Пользователь не может сломать запрос через значение параметра.

---

## Шаг ⑦ — `fetchall()` с лимитом строк

Курсор — это итератор. `fetchall()` тянет все строки в память сразу. Поэтому вместо него используем цикл с ранним выходом:

```python
for i, row in enumerate(cursor.fetchall()):
    if i >= limit:          # достигли лимита
        break               # больше не читаем
    rows.append(dict(zip(columns, row)))
```

Лимит по умолчанию — 100 строк (`settings.max_rows`). Инструмент `sql_tool.py` ещё до коннектора добавляет `TOP 100` в сам SQL — это двойная защита. Коннектор не знает был ли `TOP` в запросе, поэтому страхуется сам.

---

## Итоговый путь одного вызова

```
execute("prod", "BankingDB", "SELECT * FROM debt WHERE id = ?", params=(123,))
  ↓
① safety check          — "SELECT" → OK
  ↓
② ключ ("prod","BankingDB") в _pool?
  ↓ нет
⑤ pyodbc.connect(conn_str)    → Connection
   _pool[("prod","BankingDB")] = conn
  ↓
⑥ cursor.execute(sql, (123,)) — параметр уходит отдельно
  ↓
⑦ fetchall() с лимитом 100 строк
  ↓
[{"id": 123, "status": "active", ...}]
```

При следующем запросе к той же паре `(prod, BankingDB)` шаги ② → ④ → ⑥ → ⑦: пул находит живое соединение, ping проходит, сразу идём к выполнению.