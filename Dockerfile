# 應用程式映像（見 AGENTS.md 第 9 節與 container-runtime 規格）
#
# - Python 版本與本機開發一致（.python-version 為 3.14），並明確指定 Debian 12：
#   Microsoft ODBC Driver 18 官方支援 Debian 12，避免基底映像悄悄換成別的 Debian 版本而壞掉。
# - 以非 root 使用者執行；映像內沒有 .env、.git、測試與文件（見 .dockerignore）。
# - 以 gunicorn（gthread）執行，不使用 Flask 開發伺服器；設定與機密一律來自環境變數。
FROM python:3.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

# unixODBC 與 Microsoft ODBC Driver 18 for SQL Server（pyodbc 連線 SQL Server 需要；接受 Microsoft 的 EULA）
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg unixodbc \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
 && apt-get purge -y --auto-remove curl gnupg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY gunicorn.conf.py wsgi.py alembic.ini ./
COPY app ./app
COPY migrations ./migrations

RUN useradd --uid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app
USER app

EXPOSE 8080

# 只確認行程存活（不碰資料庫）；平台層級的就緒檢查請使用 /readyz
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8080'), timeout=3)"]

# 設定寫在 gunicorn.conf.py；--preload 讓設定錯誤（例如缺少必要機密）在主程序就失敗並以非零碼結束
CMD ["gunicorn", "--config", "gunicorn.conf.py", "wsgi:app"]
