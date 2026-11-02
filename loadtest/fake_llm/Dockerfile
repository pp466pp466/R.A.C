# 假模型伺服器的映像（見 server.py 與 fake-model-server 規格）
#
# - 獨立於主映像：建置內容是這個目錄本身（`docker build loadtest/fake_llm`，雲端為 `gcloud builds submit loadtest/fake_llm`），
#   只有 server.py 與 requirements.txt，沒有應用程式程式碼、資料庫驅動與任何機密。
# - 以非 root 使用者執行；測試金鑰（FAKE_LLM_API_KEY）與延遲、錯誤率由環境變數在部署時設定，不寫在映像裡。
# - 單一 worker、多執行緒：模擬延遲與逾時是睡眠，每個進行中的請求佔一條執行緒（FAKE_LLM_THREADS 調整）；
#   單一 worker 也讓彙總日誌的請求數是整體的數字。
FROM python:3.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    FAKE_LLM_THREADS=200

WORKDIR /srv

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY server.py ./

RUN useradd --uid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin fake
USER fake

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8080'), timeout=3)"]

# 設定不合法（例如缺少 FAKE_LLM_API_KEY）在載入時就失敗並以非零碼結束，而不是帶著錯誤設定運行
CMD ["sh", "-c", "exec gunicorn --worker-class gthread --workers 1 --threads ${FAKE_LLM_THREADS} --timeout 120 --no-control-socket --bind 0.0.0.0:${PORT} --preload 'server:create_fake_llm_app()'"]
