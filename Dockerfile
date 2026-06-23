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
#
# Ключ кладём в /usr/share/keyrings/ и явно ссылаемся через signed-by —
# это требование нового apt (Debian 12+) с sqv-верификатором.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates gcc g++ \
 && curl -sSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] \
https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql17 unixodbc-dev \
 && apt-get purge -y --auto-remove gnupg \
 && rm -rf /var/lib/apt/lists/*

# --- Python-зависимости (отдельный слой ради кеша сборки) ---------------
COPY requirements.txt ./
RUN pip install -r requirements.txt

# --- Предзагрузка эмбеддинг-модели в образ ------------------------------
# Модель эмбеддингов кешируем на этапе сборки, чтобы в рантайме не тянуть
# её из интернета. Сам RAG-индекс (chroma_db/) в репозитории не хранится:
# он строится при первом старте приложения (warmup → build_index_if_empty)
# и сохраняется на диск контейнера.
RUN python -c "from sentence_transformers import SentenceTransformer; \
SentenceTransformer('intfloat/multilingual-e5-small')"

# --- Код приложения -----------------------------------------------------
# Если рядом окажется локально собранный chroma_db/, он скопируется и будет
# переиспользован (build_index_if_empty увидит непустой индекс и пропустит
# индексацию). Если его нет — индекс соберётся на старте.
COPY . .

EXPOSE 8000

# Лёгкая проверка живости веб-сервера
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health', timeout=4).status==200 else 1)" \
  || exit 1

CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8000"]