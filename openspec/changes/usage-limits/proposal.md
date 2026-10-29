# Proposal

## Why

C-3 讓已登入且通過年齡確認的會員可以和 AI 客服對話，但**每一則訊息都會呼叫 Gemini，而目前沒有任何用量保護**：一位使用者可以每秒送出訊息直到金鑰的免費額度用完，也沒有全站的上限，被安全過濾反覆阻擋的帳號也不會被暫停。`docs/路線圖-roadmap.md` 與 C-3 的 proposal 都寫了「C-4 完成前不得對公開網路開放客服」。

C-4（`usage-safety-ops`）原本把用量、管理員後台、個資生命週期（刪除帳號、匯出、保留期限清理）與告警放在同一個 change，範圍約為 C-3 的 2 到 3 倍。使用者決定把它拆成三個依序進行的 change；這是第一個，也是客服能對外開放的**最低門檻**：C-4a `usage-limits`（本 change）、C-4b `admin-console`（`/admin`、停權、調整會員等級、稽核）、C-4c `privacy-lifecycle`（刪除帳號、匯出、保留期限清理、告警）。

## What Changes

- **每位使用者的限制**（AGENTS.md 第 7、10 節）：依會員等級的每日對話輪數（30 / 100 / 300）、每分鐘訊息數（6 / 10 / 20）、對話數上限（20 / 100 / 500）；送出訊息與建立對話在做任何事之前先檢查，超額時**不寫入任何訊息、不呼叫模型**，並以繁體中文說明原因與何時可再試。冪等重送（已有回覆的訊息）不受限制也不計數。
- **全站每日用量上限**：新增環境變數 `USAGE_DAILY_TOKEN_LIMIT`（AGENTS.md 第 10 節的 token 上限）與 `USAGE_DAILY_CALL_LIMIT`（呼叫次數上限）；以 `llm_call_logs` 彙總當日（Asia/Taipei）全部呼叫的 token 與筆數，任一項達 80% 記錄警告事件，任一項達 100% 以繁體中文回覆系統忙碌文案並停止呼叫模型（摘要折疊同樣延後）。**呼叫次數上限是依使用者提供的供應商配額加上的**：目前使用的 `gemini-3.5-flash-lite` 免費層為 15 RPM、25 萬 TPM、**每日 500 次請求（RPD）**，而每次請求不論大小都算一次，單靠 token 上限擋不住 RPD（前幾輪的呼叫只有約 1,800 token，500 次只需約 90 萬 token）。
- **安全過濾累計暫停**：同一使用者一日內被內容安全過濾阻擋達 3 次，暫停客服對話 1 小時並記錄事件（供 C-4b 的後台檢視）。暫停狀態由 `llm_call_logs` 推得，不新增使用者欄位。
- **額度顯示**：新增 `GET /api/cs/usage`（今日已用與上限、每分鐘上限、對話數與上限、暫停截止時間）；`/cs` 的 Sidebar 底部顯示今日剩餘輪數；前端對新的錯誤代碼做對應處理（不再自動重送已知無法立即成功的訊息）。
- **單一來源**：各等級的限制集中在 `app/services/quota_policy.py` 的常數表，客服 system prompt 中的額度數字（目前是 `cs_prompt._QUOTA_FACTS` 的手抄值）改為由同一份常數產生，並以測試守住一致。
- **資料庫**：migration `0006` 只新增兩個索引（`llm_call_logs.created_at`、`chat_messages.created_at`）支援全站彙總與每分鐘計數；不新增欄位或資料表，符合 expand/contract。
- **不在範圍（留給後續 change）**：管理員後台、停權與解除、調整會員等級、稽核（C-4b）；刪除帳號、匯出、保留期限清理、監控與告警管道（C-4c）；個別限制的後台調整（目前為程式常數，管理員只能改使用者的等級）；RPG 的同時串流數限制（RPG 系列）；訂單相關（C-6）。

## Capabilities

### New Capabilities
- `cs-usage-limits`: 每位使用者依會員等級的每分鐘訊息數、每日對話輪數、對話數上限；檢查順序、錯誤代碼與繁體中文文案、冪等重送不計數、`GET /api/cs/usage`、限制的單一來源。
- `global-usage-budget`: 全站每日用量上限（token 與呼叫次數）——設定、以帳本彙總的方式、80% 警告、100% 的系統忙碌回應與摘要延後。
- `cs-safety-suspension`: 同一使用者一日內被安全過濾阻擋達 3 次即暫停對話 1 小時——計算方式、暫停期間的行為、記錄。

### Modified Capabilities
- `cs-chat-ui`: 新增需求——前端對額度、暫停與系統忙碌的錯誤代碼有專屬處理，並在 Sidebar 顯示今日剩餘輪數。
- `app-configuration`: 新增需求——`USAGE_DAILY_TOKEN_LIMIT` 與 `USAGE_DAILY_CALL_LIMIT` 的驗證與預設值。
- `service-observability`: 新增需求——用量事件（拒絕、全站預算警告、暫停）只輸出白名單欄位，不含訊息文字。

## Impact

- **新增程式**：`app/services/quota_policy.py`（等級限制常數、Asia/Taipei 日界）、`app/services/usage_service.py`（檢查與彙總）、`app/repositories/` 對帳本與訊息的彙總方法、`app/observability/usage_events.py`、migration `0006`、`GET /api/cs/usage`。
- **修改程式**：`app/services/chat_service.py`（送出與建立對話前的檢查）、`app/services/summary_service.py`（預算用完時延後）、`app/services/cs_prompt.py`（額度事實改由常數產生）、`app/services/errors.py`、`app/controllers/errors.py`（新的錯誤類型與狀態碼）、`app/config.py`、`.env.example`、`app/observability/json_logging.py`（兩個新的白名單欄位）、`app/services/registry.py`、前端 `cs-sync.js`、`chat.js`、`cs.css` 與 `cs/chat.html`、README、`docs/` 設計文件。
- **既有行為**：超額的訊息現在會被拒絕（429／503／403）；既有使用者在一般使用下不會受影響（30 輪／日、6 則／分鐘）。第三次真實驗收的 12 則對話腳本在 `free` 等級下，每分鐘上限 6 則會擋下間隔小於 10 秒的連續送出——驗收腳本維持 12 秒間隔即可。
- **相依套件**：不新增。
- **外部**：全站上限的預設值依使用者提供的 `gemini-3.5-flash-lite` 免費層配額（15 RPM、25 萬 TPM、500 RPD）設定，換模型或換供應商（使用者計畫正式環境改用 OpenAI）時須重新設定；不涉及新的真實 Gemini 呼叫。**15 RPM 是整個專案共用**：free 會員單人上限是每分鐘 6 則，三位以上同時使用就可能碰到供應商的 429，結果是降級文案而不是額度訊息；本 change 不加全站每分鐘閘門（見 design 的風險）。
- **上線限制**：本 change 完成後客服才有額度保護，但仍缺管理員停權與個資生命週期（C-4b、C-4c）；對外開放前還須通過 C-5 的上線前關卡。
