"""gunicorn 設定（見 AGENTS.md 第 9 節）。

worker 數與執行緒數是初始值（客服 D-13 的估算），C-5 會依負載測試結果調整；皆可用環境變數覆寫。
客服與 RPG 串流日後使用各自獨立的 worker 池，這份設定是客服池。
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"

# gthread：相容同步程式碼與 SQLAlchemy／pyodbc；一個請求佔用一條執行緒
worker_class = "gthread"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "8"))

# 單次請求總預算 30 秒（含重試），因此 worker 逾時要比它大
timeout = 60
graceful_timeout = 30   # 收到終止訊號後，等進行中的請求完成的時間
keepalive = 5

# 讓設定驗證錯誤在主程序載入應用程式時就失敗（缺少必要機密 → 以非零碼結束，而不是帶著錯誤設定運行）
preload_app = True

# 請求日誌由應用程式自己輸出（JSON、不含查詢字串），所以關閉 gunicorn 的存取日誌
accesslog = None
errorlog = "-"
loglevel = "info"

# 容器內的暫存目錄使用記憶體，避免 /tmp 造成 worker 心跳卡住
worker_tmp_dir = "/dev/shm"
