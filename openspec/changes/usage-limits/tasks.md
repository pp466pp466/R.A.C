# Tasks

## 1. 限制表、設定與單一來源

- [x] 1.1 新增 `app/services/quota_policy.py`：各等級限制的常數表（每日輪數、每分鐘、對話數）、不認得的等級退回 free、Asia/Taipei 日界（沿用 `age_policy.TAIPEI`）轉為 UTC 起訖；單元測試涵蓋三個等級、未知等級、日界前後一秒與跨日。驗證：`pytest tests/test_quota_policy.py` 通過
- [x] 1.2 `app/config.py` 新增 `USAGE_DAILY_TOKEN_LIMIT`（預設 1,000,000）與 `USAGE_DAILY_CALL_LIMIT`（預設 450；依 `gemini-3.5-flash-lite` 免費層 500 RPD 的 90%）：正整數、上限檢查、錯誤訊息指名變數，加入設定物件，`.env.example` 加入兩個變數名稱與依據說明；測試涵蓋預設、自訂、`0`、負數、非數字。驗證：`pytest tests/test_config*.py` 通過，`.env.example` 的測試仍通過
- [x] 1.3 `app/services/cs_prompt.py` 的 `_QUOTA_FACTS` 改由限制表產生；測試斷言提示詞中每個等級的三個數字都等於限制表。驗證：`pytest tests/test_cs_prompt.py` 通過，提示詞文字（除數字來源外）沒有改變

## 2. 資料層：索引與彙總查詢

- [x] 2.1 兩個模型各加索引（`llm_call_logs.created_at`、`chat_messages.created_at`）並新增 migration `0006`（只用 Alembic op API、不含原生 SQL、啟用 batch）；在 SQLite 上驗證 upgrade／downgrade 往返與 `alembic check` 無漂移。驗證：migration 測試與 `tests/test_design_docs.py` 的 migration 版本檢查
- [x] 2.2 `LlmCallLogRepository` 新增彙總方法：某使用者今日 `counted` 的對話列數、某使用者今日對話呼叫被擋的最新時間與次數、全站今日 token 加總（`COALESCE`，空值為 0）與筆數（同一個查詢）；`ChatRepository` 新增：最近 60 秒某使用者被接受的訊息時間（取前 N 筆）、某使用者的對話數、某使用者處理中（未過期且排除指定對話）的對話數。全部只用 ORM、方法簽名帶 `user_id`（全站加總除外並在 docstring 說明）。驗證：repository 測試涵蓋日界、空值、他人的資料不被計入、摘要被擋不計入
- [x] 2.3 SQL Server 相容檢查：彙總以 `== true()`／`func.coalesce` 表達（不用 `.is_(True)`），並在本機 SQL Server 的開發資料庫備份後執行 `alembic upgrade head`，確認 `0006` 套用成功、索引存在、`/readyz` 回 200。驗證：`alembic current` 為 `0006`，備份檔 `RESTORE VERIFYONLY` 通過

## 3. UsageService、錯誤與事件

- [x] 3.1 `app/services/errors.py` 新增 `RateLimited`（含 `retry_after`）、`DailyQuotaExceeded`、`ConversationLimitReached`、`SystemBusy`、`ChatSuspended`（含 `until`）；`app/controllers/errors.py` 對應 429／429／409／503／403，429 與 503 帶 `Retry-After`，訊息為規格指定的繁體中文。驗證：controller 錯誤測試涵蓋狀態碼、代碼、標頭與文案，且回應不含內部細節
- [x] 3.2 `app/observability/usage_events.py` 新增 `usage.denied`、`usage.global_budget_warning`、`cs.chat_suspended`（函式不接收文字內容）；`json_logging.ALLOWED_FIELDS` 加入 `used`、`limit`。驗證：日誌測試證明事件只輸出白名單欄位且 `used`／`limit` 為整數，現有的「不洩漏」測試仍通過
- [x] 3.3 新增 `UsageService`（`check_send`、`check_create_conversation`、`snapshot`、`global_budget_exhausted`、`record_block_outcome`），依序檢查暫停 → 每分鐘 → 每日 → 全站，並把 80% 警告與拒絕事件接上；用假時鐘做單元測試涵蓋每個限制的邊界（剛好達上限、少一個、視窗滑動、`Retry-After` 秒數、處理中保留名額、跨日、暫停期滿與期滿後再被擋、摘要被擋不計入、全站 token 或呼叫次數任一項達上限、80% 警告只在兩項都未達上限時記錄）。驗證：`pytest tests/test_usage_service.py` 通過
- [x] 3.4 `registry.py` 組裝 `UsageService` 與背景摘要用的預算檢查；`tests/architecture/` 的分層測試通過（Service 不匯入 Flask 與 Controller、Repository 不匯入 Service）。驗證：`pytest tests/architecture` 通過

## 4. 接上對話流程與用量端點

- [x] 4.1 `ChatService.send_message` 在取得處理標記之前呼叫 `check_send`；已有回覆的重送直接回傳且不檢查；已儲存但沒有回覆的訊息重送略過每分鐘檢查。拒絕時不寫入訊息、不設標記、不呼叫模型、不新增帳本。驗證：整合測試以假供應商斷言每種拒絕之後訊息數、帳本筆數與處理標記都不變，並涵蓋檢查順序（同時違反多項時的回應）
- [x] 4.2 回覆寫入後，若該次降級原因是被安全過濾阻擋，呼叫 `record_block_outcome`（第三次被擋時記錄暫停事件）；`create_conversation` 呼叫 `check_create_conversation`（冪等重送不受影響、刪除後釋出名額）。驗證：測試涵蓋第三次被擋後的下一則為 403 `chat_suspended`、隔日恢復、對話數上限與冪等
- [x] 4.3 新增 `GET /api/cs/usage`（只需登入、只含自己的用量與暫停截止時間、不含全站資訊）；權限測試證明未登入 401、無法讀到他人資料，並套用 `tests/support/idor.py` 的框架。驗證：`pytest tests/test_cs_api*.py tests/test_idor*.py` 通過
- [x] 4.4 `SummaryService` 在全站預算用完時延後（不呼叫模型、已摘要序號不變）；測試涵蓋預算用完與恢復後補做。驗證：`pytest tests/test_summary_service.py` 通過
- [x] 4.5 並行與查詢數：以多執行緒送出測試證明並行超額不超過並行數、處理中的對話預留名額；以查詢計數測試守住一次送出的聚合查詢數上限。驗證：對應測試通過且在 SQLite 檔案資料庫上穩定重複 5 次

## 5. 前端

- [x] 5.1 `cs-sync.js` 的 `classify` 先看代碼：`daily_quota_exceeded`、`chat_suspended`、`conversation_limit_reached` 歸為 `fail`，`rate_limited`、`system_busy` 沿用 `retry`；`tests/js/` 的同步引擎測試涵蓋每個代碼與既有的 429／503 行為不變。驗證：`node --test tests/js` 通過
- [x] 5.2 `chat.js`／`cs/chat.html`／`cs.css`：暫停時顯示截止時間並停用輸入、無法送出的訊息顯示伺服器說明、Sidebar 底部顯示今日剩餘輪數（每則回覆後更新，失敗則隱藏）；沿用 `v-if`／`v-show`、不使用 Bootstrap JS 與 `v-html`，色彩符合規範。驗證：JS 測試涵蓋剩餘輪數的顯示與隱藏、暫停橫幅
- [x] 5.3 瀏覽器驗收（Edge，桌面與手機寬度）：以假供應商啟動伺服器，走過「用完今日額度」「暫停」「每分鐘限制」三種畫面並檢視截圖，確認版面與文案。驗證：截圖已檢視，缺陷已修正

## 6. 驗收與文件

- [x] 6.1 全部測試通過（`pytest`、`node --test tests/js`），並以假供應商的端到端測試走完「一般使用不受影響」：free 會員 30 輪內不被擋、第 31 輪被擋、隔日恢復。驗證：完整測試套件的結果寫入 README 的驗收紀錄
- [x] 6.2 README 更新：`USAGE_DAILY_TOKEN_LIMIT` 與 `USAGE_DAILY_CALL_LIMIT` 的說明與預設值依據（15 RPM、25 萬 TPM、500 RPD 與換模型時須重設）、新端點與錯誤代碼表、本機資料庫升級紀錄、「C-4 之前不得對公開網路開放」的現況改為「C-4a 完成，仍缺 C-4b、C-4c」。驗證：`pytest tests/test_readme.py` 通過
- [x] 6.3 更新 docs/ 對應的設計文件（對照表見 docs/README.md）：`客服對話-chat-service.md`（送出流程加入檢查、`GET /api/cs/usage`）、`模型呼叫層-llm-layer.md`（全站預算與帳本用途）、`資料模型-data-model.md`（migration 0006 與索引）、`前端結構-frontend.md`（新增的前端行為）、`營運與部署-operations.md`（新環境變數與事件）、`路線圖-roadmap.md`（C-4 拆成 C-4a／b／c 與狀態）；並讓 `tests/test_design_docs.py` 通過
