"""
Менеджер подключений к MS SQL серверам.

Задача этого модуля — спрятать всю работу с pyodbc за чистым API.
Остальной код просто вызывает connector.execute(server, db, sql).

Ключевые решения:
  - Пул подключений: одно подключение на пару (server, database),
    переиспользуем его вместо открытия нового каждый раз
  - Контекстный менеджер: подключение автоматически закрывается
    при ошибке
  - Read-only guard: блокируем мутирующие операторы на уровне
    коннектора — второй рубеж после Pydantic-валидатора
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Generator

import pyodbc

from core.config import settings, ServerConfig
from core.schemas.sql_safety import find_mutations

logger = logging.getLogger(__name__)


# =============================================================
# Исключения
# =============================================================

class ConnectorError(Exception):
    """Базовое исключение коннектора."""

class ServerNotFoundError(ConnectorError):
    """Запрошенный сервер не найден в конфигурации."""

class UnsafeQueryError(ConnectorError):
    """Попытка выполнить мутирующий запрос."""


# =============================================================
# Менеджер подключений
# =============================================================

class DBConnector:
    """
    Управляет подключениями к нескольким MS SQL серверам.
    """

    def __init__(self) -> None:
        # Кеш открытых подключений: ключ = (server_alias, database_name)
        self._pool: dict[tuple[str, str], pyodbc.Connection] = {}

        # Индекс серверов по alias для быстрого доступа
        self._servers: dict[str, ServerConfig] = {
            s.alias: s for s in settings.servers
        }

    # == Поиск конфигурации ====================================

    def get_server_config(self, server_alias: str) -> ServerConfig:
        """Возвращает конфигурацию сервера по алиас."""
        config = self._servers.get(server_alias)
        if config is None:
            available = list(self._servers.keys())
            raise ServerNotFoundError(
                f"Сервер '{server_alias}' не найден. "
                f"Доступные: {available}"
            )
        return config

    def list_servers(self) -> list[str]:
        """Список всех настроенных серверов."""
        return list(self._servers.keys())

    def list_databases(self, server_alias: str) -> list[str]:
        """Список баз данных на сервере (из конфига)."""
        config = self.get_server_config(server_alias)
        return [db.name for db in config.databases]

    # == Работа с подключениями ================================

    def _get_or_create_connection(
        self, server_alias: str, database: str
    ) -> pyodbc.Connection:
        """
        Возвращает существующее подключение из пула или создаёт новое.
        Проверяет живость соединения перед возвратом.
        """
        key = (server_alias, database)

        # Проверяем, жив ли кешированный коннект
        if key in self._pool:
            try:
                self._pool[key].execute("SELECT 1")  # ping
                return self._pool[key]
            except pyodbc.Error:
                logger.warning(f"Подключение {key} умерло, переподключаемся...")
                del self._pool[key]

        # Создаём новое подключение
        config = self.get_server_config(server_alias)
        conn_str = config.get_connection_string(database)

        logger.debug(f"Открываем подключение: {server_alias}/{database}")
        conn = pyodbc.connect(
            conn_str,
            timeout=settings.query_timeout,
        )
        conn.autocommit = True   # важно для read-only режима

        self._pool[key] = conn
        return conn

    @contextmanager
    def get_connection(
        self, server_alias: str, database: str
    ) -> Generator[pyodbc.Connection, None, None]:
        """
        Контекстный менеджер для работы с подключением.
        
        with connector.get_connection("prod", "BankingDB") as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT ...")
        """
        conn = self._get_or_create_connection(server_alias, database)
        try:
            yield conn
        except pyodbc.Error as e:
            # При ошибке убираем подключение из пула — следующий запрос
            # создаст новое
            key = (server_alias, database)
            self._pool.pop(key, None)
            raise ConnectorError(f"Ошибка БД [{server_alias}/{database}]: {e}") from e

    # == Выполнение запросов ===================================

    def _check_query_safety(self, sql: str) -> None:
        """
        Второй рубеж защиты от мутирующих запросов.
        Первый — Pydantic-валидатор в GeneratedSQL.
        """
        found = find_mutations(sql)
        if found:
            raise UnsafeQueryError(
                f"Запрос содержит запрещённые операторы: {found}"
            )

    def execute(
        self,
        server_alias: str,
        database:     str,
        sql:          str,
        params:       tuple = (),
        max_rows:     int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Выполняет SELECT-запрос и возвращает список словарей.
        
        Args:
            server_alias: псевдоним сервера из конфига (например "prod")
            database:     имя базы данных
            sql:          SQL-запрос (только SELECT)
            params:       позиционные параметры для pyodbc (?)
            max_rows:     лимит строк (по умолчанию из settings.max_rows)
        
        Returns:
            [{"column": value, ...}, ...]
        
        Raises:
            UnsafeQueryError: если запрос содержит мутирующие операторы
            ConnectorError:   при ошибке подключения или выполнения
        """
        self._check_query_safety(sql)

        limit = max_rows if max_rows is not None else settings.max_rows

        with self.get_connection(server_alias, database) as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)

            columns = [col[0] for col in cursor.description]
            rows = []

            for i, row in enumerate(cursor.fetchall()):
                if i >= limit:
                    logger.warning(
                        f"Результат обрезан до {limit} строк "
                        f"[{server_alias}/{database}]"
                    )
                    break
                rows.append(dict(zip(columns, row)))

            logger.debug(
                f"Запрос выполнен: {len(rows)} строк "
                f"[{server_alias}/{database}]"
            )
            return rows

    def execute_scalar(
        self,
        server_alias: str,
        database:     str,
        sql:          str,
        params:       tuple = (),
    ) -> Any:
        """
        Возвращает единственное значение (первую колонку первой строки).
        Удобно для COUNT(*), MAX(...) и т.п.
        """
        rows = self.execute(server_alias, database, sql, params, max_rows=1)
        if not rows:
            return None
        return next(iter(rows[0].values()))

    # == Закрытие =============================================

    def close_all(self) -> None:
        """Закрывает все подключения в пуле. Вызывать при завершении."""
        for key, conn in self._pool.items():
            try:
                conn.close()
                logger.debug(f"Закрыто подключение: {key}")
            except pyodbc.Error:
                pass
        self._pool.clear()


# Единственный экземпляр коннектора для всего приложения (singleton)
connector = DBConnector()