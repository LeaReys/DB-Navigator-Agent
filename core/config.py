"""
Конфигурация.

Все настройки подключений хранятся здесь.
Чувствительные данные (пароли) берём из переменных окружения.
"""

import os
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

# Загружаем .env до ручного чтения DB_SERVER_* через os.getenv()
load_dotenv(override=False)


class DatabaseConfig(BaseModel):
    """Описывает одну БД внутри SQL Serve."""
    name:        str
    description: str = ""
    # Список таблиц, которые нужно индексировать для RAG
    # если список пустой, то берём все таблицы БД
    tables_to_index: list[str] = Field(default_factory=list)


class ServerConfig(BaseModel):
    """Описывает один сервер и список баз данных на нём"""
    alias:     str            # короткое имя, например "profisql"
    host:      str            # hostname или IP
    port:      int = 1433
    driver:    str = "ODBC Driver 17 for SQL Server"
    databases: list[DatabaseConfig] = Field(default_factory=list)

    # Если используется Windows Auth — оставь пустыми
    username: str = ""
    password: str = ""

    @property
    def use_windows_auth(self) -> bool:
        return not self.username and not self.password

    def get_connection_string(self, database: str) -> str:
        """Собирает строку подключения pyodbc."""
        base = (
            f"DRIVER={{{self.driver}}};"
            f"SERVER={self.host},{self.port};"
            f"DATABASE={database};"
        )
        if self.use_windows_auth:
            return base + "Trusted_Connection=yes;"
        return base + f"UID={self.username};PWD={self.password};"


class Settings(BaseSettings):
    """
    Основные настройки приложения.
    Поля с префиксом DB_ читаются из .env файла или переменных окружения.
    """

    # == Серверы ===============================================
    servers: list[ServerConfig] = Field(default_factory=list)

    # == Лимиты безопасности ===================================
    max_rows:        int = 100    # максимум строк в одном SELECT
    query_timeout:   int = 30     # секунд на выполнение запроса

    # == OpenRouter ============================================
    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")

    openrouter_model_small: str = Field(
        default="mistralai/mistral-7b-instruct:free",
        validation_alias="OPENROUTER_MODEL_SMALL",
    )
    openrouter_model_large: str = Field(
        default="deepseek/deepseek-coder-v2-instruct:free",
        validation_alias="OPENROUTER_MODEL_LARGE",
    )

    # == Ollama ================================================
    ollama_host: str = Field(
        default="http://localhost:11434",
        validation_alias="OLLAMA_HOST",
    )

    ollama_model_small: str = Field(
        default="llama3.1:8b",          # qwen2.5:3b
        validation_alias="OLLAMA_MODEL_SMALL",
    )
    ollama_model_large: str = Field(    # qwen2.5-coder:7b
        default="deepseek-coder-v2:16b",
        validation_alias="OLLAMA_MODEL_LARGE",
    )
    ollama_think: bool = Field(
        default=False,
        validation_alias="OLLAMA_THINK",
    )

    # == LLM ==================================================
    # USE_OLLAMA=true  → локальная Ollama
    # USE_OLLAMA=false → OpenRouter
    use_ollama: bool = Field(default=False, validation_alias="USE_OLLAMA")

    # Макс. кол-во токенов в ответе от LLM — важно для контроля затрат и предотвращения слишком длинных ответов.
    llm_max_tokens: int = Field(default=8000, validation_alias="LLM_MAX_TOKENS")
    
    # LLM retry при 429
    llm_retry_max_attempts: int   = Field(default=3,   validation_alias="LLM_RETRY_MAX_ATTEMPTS")
    llm_retry_base_delay:   float = Field(default=5.0, validation_alias="LLM_RETRY_BASE_DELAY")
    llm_retry_multiplier:   float = Field(default=2.0, validation_alias="LLM_RETRY_MULTIPLIER")

    # == LangFuse =============================================
    langfuse_public_key:  str = Field(default="", validation_alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key:  str = Field(default="", validation_alias="LANGFUSE_SECRET_KEY")
    langfuse_host:        str = Field(
        default="https://cloud.langfuse.com",
        validation_alias="LANGFUSE_HOST",
    )

    # == RAG / ChromaDB ========================================
    chroma_persist_dir: str = str(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "chroma_db")
    )
    rag_top_k: int = 5               # сколько чанков возвращать при поиске

    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # == Унифицированный доступ к именам моделей ===============
    # Используй эти свойства в коде — они сами выбирают нужный провайдер.
    # Так не нужно писать if/else везде где нужно имя модели.
    @property
    def model_small(self) -> str:
        """Имя малой модели для активного провайдера."""
        return self.ollama_model_small if self.use_ollama else self.openrouter_model_small
 
    @property
    def model_large(self) -> str:
        """Имя большой модели для активного провайдера."""
        return self.ollama_model_large if self.use_ollama else self.openrouter_model_large
 
    @property
    def active_provider(self) -> str:
        """Имя активного провайдера — для логов и UI."""
        return "ollama" if self.use_ollama else "openrouter"


def _build_default_settings() -> Settings:
    """
    Создаёт настройки по умолчанию.
    
    Серверы и БД читаем из переменных окружения — 
    так проще менять конфигурацию без изменения кода.
    
    Формат переменных окружения:
        DB_SERVER_1_HOST=192.168.1.10
        DB_SERVER_1_ALIAS=prod
        DB_SERVER_1_USERNAME=sa
        DB_SERVER_1_PASSWORD=secret
        DB_SERVER_1_DATABASES=BD1,BD2
    """
    servers: list[ServerConfig] = []

    for i in range(1, 6):   # поддерживаем до 5 серверов
        host = os.getenv(f"DB_SERVER_{i}_HOST", "")
        if not host:
            break

        db_names = os.getenv(f"DB_SERVER_{i}_DATABASES", "").split(",")
        databases = [
            DatabaseConfig(name=db.strip())
            for db in db_names
            if db.strip()
        ]

        servers.append(ServerConfig(
            alias    = os.getenv(f"DB_SERVER_{i}_ALIAS",    f"server_{i}"),
            host     = host,
            port     = int(os.getenv(f"DB_SERVER_{i}_PORT", "1433")),
            username = os.getenv(f"DB_SERVER_{i}_USERNAME", ""),
            password = os.getenv(f"DB_SERVER_{i}_PASSWORD", ""),
            databases= databases,
        ))

    # Если серверов нет в env - добавляем заглушку
    if not servers:
        servers = [
            ServerConfig(
                alias     = "dev",
                host      = os.getenv("DB_HOST", "localhost"),
                username  = os.getenv("DB_USER", ""),
                password  = os.getenv("DB_PASS", ""),
                databases = [
                    DatabaseConfig(
                        name        = os.getenv("DB_NAME", "BankingDB"),
                        description = "Основная банковская БД (dev)",
                    )
                ],
            )
        ]

    return Settings(servers=servers)


# Единственный экземпляр настроек для всего приложения
settings = _build_default_settings()