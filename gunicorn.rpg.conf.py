"""gunicorn 設定：RPG 串流池（見 AGENTS.md 第 9 節與 rpg-turn-engine 的 design D14）。

客服池的設定在 gunicorn.conf.py；RPG 的事件串流（`/api/game/*`）使用這一份獨立的設定，讓長連線不佔用客服的執行緒。
映像同時包含兩份設定，Dockerfile 預設啟動客服池，RPG 池由部署指定：
`gunicorn --config gunicorn.rpg.conf.py wsgi:app`（路徑如何分流到兩個池，屬後續的部署工作）。

worker 數與執行緒數是初始值，RC-4 的負載測試會校正；皆可用環境變數覆寫。每個進行中的串流佔用一條請求執行緒，
外加一條上游讀取執行緒，所以執行緒數比客服池多。
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"

# gthread：相容同步程式碼與 SQLAlchemy／pyodbc；一個串流請求佔用一條執行緒直到結束或斷線
worker_class = "gthread"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "16"))

# 一個回合最壞約 150 秒（被擋兩次各最多 60 秒，加狀態呼叫 30 秒），前端總逾時 180 秒；worker 逾時必須大於單次生成上限與心跳間隔
timeout = 180
graceful_timeout = 60   # 收到終止訊號後，等進行中的回合寫回（完成、結算或退款）的時間
keepalive = 5

# 不使用 gunicorn 26 起的控制通道（gunicornc）：非 root 使用者沒有家目錄，預設路徑建不出來（見 gunicorn.conf.py）
control_socket_disable = True

# 讓設定驗證錯誤在主程序載入應用程式時就失敗（缺少必要機密 → 以非零碼結束，而不是帶著錯誤設定運行）
preload_app = True

# 請求日誌由應用程式自己輸出（JSON、不含查詢字串），所以關閉 gunicorn 的存取日誌
accesslog = None
errorlog = "-"
loglevel = "info"

# 容器內的暫存目錄使用記憶體，避免 /tmp 造成 worker 心跳卡住
worker_tmp_dir = "/dev/shm"
