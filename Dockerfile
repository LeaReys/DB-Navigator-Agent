# =========================================================
# DB Navigator Agent — образ веб-приложения (FastAPI + агент)
# =========================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf-cache

WORKDIR /app

# --- Системные зависимости: ODBC Driver 17 для MS SQL Server -------------
# Driver 17 выбран намеренно: по умолчанию не требует TLS-сертификата,
# поэтому подключение к demo-серверу работает «из коробки».
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates apt-transport-https gcc g++ \
 && curl -sSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor > /etc/apt/trusted.gpg.d/microsoft.gpg \
 && curl -sSL https://packages.microsoft.com/config/debian/12/prod.list \
        > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql17 unixodbc-dev \
 && apt-get purge -y --auto-remove gnupg apt-transport-https \
 && rm -rf /var/lib/apt/lists/*

# --- Python-зависимости (отдельный слой ради кеша сборки) ---------------
COPY requirements.txt ./
RUN pip install -r requirements.txt

# --- Предзагрузка эмбеддинг-модели в образ ------------------------------
# RAG-индекс уже лежит в репозитории (chroma_db/), а модель эмбеддингов
# кешируем на этапе сборки — в рантайме интернет для RAG не нужен.
RUN python -c "from sentence_transformers import SentenceTransformer; \
SentenceTransformer('intfloat/multilingual-e5-small')"

# --- Код приложения (включая готовый chroma_db/) ------------------------
COPY . .

EXPOSE 8000

# Лёгкая проверка живости веб-сервера
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health', timeout=4).status==200 else 1)" \
  || exit 1

CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8000"]
