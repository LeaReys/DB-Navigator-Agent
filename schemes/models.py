"""
Структурированные выходы Pydantic DB Navigator Agent
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field, field_validator


# Паттерн для проверки SQL на мутирующие операторы.
_MUTATION_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE"
    r"|EXEC|EXECUTE|GRANT|REVOKE|MERGE)\b",
    re.IGNORECASE,
)


# =============================================
# 1. КЛАССИФИКАЦИЯ ЗАПРОСА (выход роутера)
# =============================================

class QueryType(str, Enum):
    """Тип входящего запроса — определяет ветку графа."""
    NAVIGATION = "navigation"   # "Где найти X?"
    SCHEMA     = "schema"       # "Структура таблицы debt?"
    SCRIPT     = "script"       # "Напиши скрипт для X"
    DATA       = "data"         # "Какой статус у должника 123?"
    UNKNOWN    = "unknown"      # не удалось классифицировать
    UNSAFE     = "unsafe"       # содержит просьбы об изменениях/удалениях
    


class ClassificationResult(BaseModel):
    """Выход узла classify_intent."""
    query_type: QueryType
    confidence: float = Field(ge=0.0, le=1.0, description="Уверенность классификатора")
    reasoning: str    = Field(description="Краткое объяснение решения")
    # Подсказки, извлечённые из запроса
    mentioned_tables:   list[str] = Field(default_factory=list)
    mentioned_entities: list[str] = Field(default_factory=list, description="id, имена и пр.")

    model_config = {"use_enum_values": True}


# =============================================
# 2. ВЫХОДЫ ИНСТРУМЕНТОВ (tools)
# =============================================

class ToolStatus(str, Enum):
    SUCCESS = "success"
    ERROR   = "error"
    EMPTY   = "empty"       # запрос выполнен, но данных нет


class ToolResult(BaseModel):
    """Базовый класс для всех tool-выходов."""
    status:    ToolStatus
    tool_name: str
    error_msg: str | None = None

    model_config = {"use_enum_values": True}


# --- Tool: search_metadata (RAG) ---

class MetadataChunk(BaseModel):
    """Один найденный фрагмент метаданных из vector store."""
    table_name:  str
    server:      str
    database:    str
    description: str
    score:       float = Field(ge=0.0, le=1.0, description="Косинусное сходство")
    columns:     list[str] = Field(default_factory=list)


class MetadataSearchResult(ToolResult):
    """Выход tool search_metadata."""
    tool_name: str = "search_metadata"
    query:     str
    chunks:    list[MetadataChunk] = Field(default_factory=list)


# --- Tool: get_table_schema ---

class ColumnInfo(BaseModel):
    """Информация об одной колонке таблицы."""
    name:        str
    data_type:   str
    is_nullable: bool
    is_pk:       bool = False
    is_fk:       bool = False
    default:     str | None = None
    description: str | None = None


class TableSchemaResult(ToolResult):
    """Выход tool get_table_schema."""
    tool_name: str = "get_table_schema"
    server:    str
    database:  str
    table:     str
    columns:   list[ColumnInfo] = Field(default_factory=list)
    row_count: int | None = None  # приблизительно, из статистики


# --- Tool: generate_sql ---

class GeneratedSQL(BaseModel):
    """Выход узла генерации SQL."""
    sql:         str   = Field(description="Готовый SQL-скрипт")
    explanation: str   = Field(description="Что делает скрипт, простыми словами")
    is_safe:     bool  = Field(description="True — только SELECT, без мутаций")
    tables_used: list[str] = Field(default_factory=list)

    @field_validator("sql")
    @classmethod
    def no_mutations(cls, v: str) -> str:
        """
        Проверка: мутирующие операторы запрещены.
            проверяем через \b...\b — граница слова.
        """
        found = _MUTATION_PATTERN.findall(v)
        if found:
            raise ValueError(
                f"SQL содержит запрещённые операторы: {sorted(set(f.upper() for f in found))}"
            )
        return v


class SQLGenerationResult(ToolResult):
    """Выход tool generate_sql."""
    tool_name: str = "generate_sql"
    generated: GeneratedSQL | None = None


# --- Tool: execute_query ---

class QueryRow(BaseModel):
    """Одна строка результата запроса."""
    data: dict[str, Any]


class ExecuteQueryResult(ToolResult):
    """Выход tool execute_query."""
    tool_name:    str = "execute_query"
    sql:          str
    rows:         list[QueryRow] = Field(default_factory=list)
    row_count:    int = 0
    columns:      list[str] = Field(default_factory=list)
    truncated:    bool = False  # True если результат обрезан (лимит строк)

    model_config = {"use_enum_values": True}


# =============================================
# 3. ФИНАЛЬНЫЙ ОТВЕТ АГЕНТА
# =============================================

class SourceReference(BaseModel):
    """Ссылка на источник данных в ответе."""
    server:   str
    database: str
    table:    str | None = None


class AgentResponse(BaseModel):
    """Финальный структурированный ответ, который видит пользователь."""
    answer:      str  = Field(description="Ответ на естественном языке")
    query_type:  QueryType
    sql:         str | None = None  # если был сгенерирован SQL
    sources:     list[SourceReference] = Field(default_factory=list)
    confidence:  float = Field(ge=0.0, le=1.0, default=1.0)
    has_data:    bool  = False  # True если вернулись реальные данные из БД

    model_config = {"use_enum_values": True}