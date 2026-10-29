# Design

## Context

C-3 已有三個可以直接利用的基礎：
- `llm_call_logs` 帳本：每次模型呼叫一筆，含 `counted`（一則訊息得到正常回覆時，最後成功的對話呼叫為真）、三種 token、`status`（含 `blocked`）、`kind`（`chat`／`summary`），索引為 `(user_id, created_at)`。失敗的列立即提交，最後一筆與助理訊息同一交易提交。
- `chat_conversations.processing_since`：同一對話同時只處理一則；60 秒過期。
- `ChatService.send_message` 的順序：閘門 → 清理輸入 → 擁有者 → 冪等查詢 → 取得處理標記 → 寫使用者訊息並提交 → 呼叫模型（無資料庫交易）→ 寫回覆。

限制：多 worker、不可用行程內記憶體（AGENTS.md 第 9 節）；只能用 ORM、不得依 JSON 欄位篩選；日界為 Asia/Taipei、資料庫存 UTC（`age_policy.TAIPEI` 為固定 UTC+8，台灣沒有日光節約時間）；會員等級不得進入 `AccessPolicy`。

## Goals / Non-Goals

**Goals:**
- 超額時**在任何寫入與模型呼叫之前**拒絕，且拒絕不留痕。
- 所有計數都從資料庫導出（帳本與訊息表），不新增計數器欄位，因此沒有「計數器與真相不一致」的問題。
- 限制的數值只有一個來源，客服自述的額度與實際限制不會分歧。
- migration 只新增索引，符合 expand/contract。

**Non-Goals:**
- 不做後台調整限制、不做依使用者客製的限制（C-4b 之後再議）。
- 不做監控告警管道（C-4c）；本 change 只輸出可被告警使用的事件。
- 不做 RPG 的同時串流限制。
- 不追求「絕對不超額」：見風險一節，超額的幅度被限制在並行請求數以內。

## Decisions

**D1　新增 `UsageService`，由 `ChatService` 注入，`AccessPolicy` 不變。**
限制屬於「額度」，不是「權限」：AGENTS.md 與 `AccessPolicy` 的檔頭都明定等級不參與權限判斷。`UsageService` 提供 `check_send(user, ...)`、`check_create_conversation(user)`、`snapshot(user)`、`global_budget_exhausted()`、`record_block_outcome(user)`。替代方案「放進 AccessPolicy」被否決，因為它會讓權限類別讀 `member_tier`。

**D2　限制表是程式常數（`app/services/quota_policy.py`），全站上限才是環境變數。**
各等級的數值是 AGENTS.md 第 7 節的規格，不是部署差異；放進環境變數會讓三個環境出現不同的「規格」。全站上限才隨模型與方案不同，所以 `USAGE_DAILY_TOKEN_LIMIT` 與 `USAGE_DAILY_CALL_LIMIT` 是環境變數。`cs_prompt._QUOTA_FACTS` 改由同一個常數表產生（先前路線圖的待辦「額度可設定時須改取自同一份設定」就此落實），並以測試守住。

**D3　檢查順序放在取得處理標記之前。**
閘門 → 清理輸入與擁有者 → 冪等回傳 → 暫停 → 每分鐘 → 每日 → 全站 → 取得處理標記（忙碌 409）。這與報告 D-20 流程圖不同（圖把「忙碌」放在速率之前）：把限制放在標記之前，拒絕時不需要釋放標記，也符合「拒絕不留痕」。代價是被忙碌擋下的請求可能先看到 429；兩者都是暫時性，可以接受。

**D4　每分鐘限制從 `chat_messages` 計數，不從帳本計數。**
帳本的最後一筆要等回覆寫完才提交，期間（最長 30 秒）其他請求看不到，並行的連發就能繞過。使用者訊息在呼叫模型之前就已提交，所以數「最近 60 秒、角色為 user、屬於這位使用者的訊息」最準確。查詢以對話表連到使用者（`chat_messages` → `chat_conversations.user_id`）；被拒絕的請求沒有寫入訊息，所以自然不計數。為了 `Retry-After`，查詢取最近 `limit` 筆的時間，第 `limit` 筆加 60 秒即最早可再送出的時間。需要 `chat_messages.created_at` 索引。已儲存但沒有回覆的訊息重送不再套用此限制（它已被接受過）。

**D5　每日輪數 = 帳本 `counted` 列數 + 處理中的其他對話。**
`counted` 已經精確表達「這則得到正常回覆」並排除降級與摘要，直接沿用。處理中的對話（`processing_since` 在 60 秒內、且不是本對話）視為已預留一個名額，避免一次開很多對話並行送出而一起通過。這是盡力而為的保留，不是鎖：兩個請求在彼此提交處理標記之前同時檢查時仍可能一起通過（超額上限為並行請求數）。

**D6　全站用量 = 當日帳本全部列的 token 加總（`COALESCE` 為 0）與筆數，各有上限。**
包含摘要、降級、失敗的嘗試，因為供應商都已計費或計配額。以 ORM 的 `func.sum(func.coalesce(...))` 與 `func.count` 在同一個查詢取得，SQLite 與 SQL Server 皆可。**為什麼多一個「呼叫次數」上限**：使用者提供目前模型（`gemini-3.5-flash-lite` 免費層）的配額為 15 RPM、25 萬 TPM、500 RPD。每次請求不論大小都耗用 RPD，而前幾輪的對話呼叫只有約 1,800 token，500 次只約 90 萬 token，所以 token 上限擋不住 RPD；TPM 不會是瓶頸（每分鐘最多 15 次、每次約 4,000 token，約 6 萬）。預設值：`USAGE_DAILY_CALL_LIMIT` = 450（RPD 的 90%，留給帳本以外的呼叫與估算誤差）、`USAGE_DAILY_TOKEN_LIMIT` = 1,000,000（一般對話約 330 輪，通常先於呼叫次數用完）。AGENTS.md 只要求 token 上限，呼叫次數上限是依供應商配額的**補充**，不與任何規範衝突。需要 `llm_call_logs.created_at` 索引（既有索引的第一欄都不是時間）。每次送出都做一次聚合：當日列數有限，且在無法使用行程內快取的約束下是最單純的做法；若日後列數成為問題，再以資料庫中的每日彙總列取代（屬於新的 change）。80% 警告沒有「每日只記一次」的共享狀態可用，所以每次越過門檻的請求都記一筆；告警層（C-4c）以時間窗去重。

**D7　安全過濾暫停從帳本推得，不加使用者欄位。**
查今日 `kind='chat'` 且 `status='blocked'` 的列：數量 ≥ 3 時，暫停截止 = 最新一筆的 `created_at` + 1 小時。暫停期間無法再產生新的被擋列（請求在呼叫前就被拒絕），所以「最新一筆 + 1 小時」自然在期滿後解除；期滿後若再被擋，最新一筆更新，等於再次暫停。AGENTS.md 第 10 節對客服的原文是「被內容安全過濾阻擋達 3 次」，未區分輸入與輸出，所以**兩者都計**；RPG 因規範另有明文（只計輸入）而不同。替代方案「在 `users` 加 `chat_suspended_until`」需要寫入與清除的路徑，也要 migration 與回填，而且和停權（`status`）的語意容易混淆。

**D8　錯誤類型與狀態碼。**
新增 `RateLimited`（429，`rate_limited`，帶 `retry_after`）、`DailyQuotaExceeded`（429，`daily_quota_exceeded`）、`ConversationLimitReached`（409，`conversation_limit_reached`）、`SystemBusy`（503，`system_busy`）、`ChatSuspended`（403，`chat_suspended`，帶 `until`）。Controller 統一轉換並在 429／503 加上 `Retry-After`。`ChatSuspended` 繼承 `Forbidden`，但不屬於前端的「閘門」代碼（那些會引導到帳號設定），前端另外處理。

**D9　`GET /api/cs/usage` 只需登入，不過閘門。**
與對話列表一致：被停權或未完成年齡確認的使用者仍能打開頁面並看到說明。回應不含全站資訊。

**D10　摘要折疊在預算用完時延後。**
`SummaryService` 注入一個「預算是否用完」的可呼叫物件（由背景工作自己的 Session 建構）；用完就直接返回，已摘要序號不變，之後的請求補做（補償式）。

**D11　日誌事件與兩個新的白名單欄位。**
新增 `app/observability/usage_events.py`：`usage.denied`（原因為固定列舉）、`usage.global_budget_warning`、`cs.chat_suspended`。為 `used`、`limit` 加入 `ALLOWED_FIELDS`（只放整數）。函式簽名不接收任何文字內容，如同 `llm_events`。

**D12　前端：用量類代碼不自動重送；Sidebar 顯示剩餘輪數。**
`cs-sync.js` 的 `classify` 先看代碼：`daily_quota_exceeded`、`chat_suspended`、`conversation_limit_reached` 歸為 `fail`（標記無法送出，顯示伺服器訊息）；`rate_limited`、`system_busy` 沿用 `retry`（既有的 10 秒自動重送，視窗最長 60 秒，可涵蓋）。暫停時 `chat.js` 顯示截止時間並停用輸入框。剩餘輪數讀 `GET /api/cs/usage`，每則回覆後更新，失敗就隱藏。

## Risks / Trade-offs

- **並行超額** → 每日與每分鐘的檢查與提交之間有時間差，並行請求可能一起通過，超額幅度不超過並行數；全站 token 與呼叫次數上限是第二道保護。若需要硬上限，須改用資料庫中的原子計數列（另案）。
- **每次送出多 3 到 4 個聚合查詢** → 都命中索引（`(user_id, created_at)`、新增的 `created_at`），資料量小；以測試量測查詢數，避免日後無意增加。全站聚合隨當日列數線性成長。
- **全站預設上限綁定目前的模型與方案** → 預設值依 `gemini-3.5-flash-lite` 免費層（15 RPM、25 萬 TPM、500 RPD）。換模型（本機與驗收之外的 `gemini-3.8-flash` 每日只有約 20 次）或正式環境改用 OpenAI 時，必須重設這兩個變數，README 與 `.env.example` 要說明依據。
- **15 RPM 是全站共用** → free 會員單人每分鐘最多 6 則，三位以上同時使用就可能一起撞到供應商的 429；此時走既有路徑（主要與備援為同一模型，直接降級，回覆降級文案、不計額度），使用者看到的是降級而不是額度訊息。本 change 不加全站每分鐘閘門，因為 AGENTS.md 沒有此要求，目前也是個人測試規模；若多人同時測試，再另案加（也是以帳本計數，做法與 D6 相同）。
- **每日 500 次與重試／備援的關係** → 重試與備援也各算一筆帳本，已被呼叫次數上限涵蓋；達到 450 次後停止呼叫，保留約 50 次餘裕給進行中的請求。
- **80% 警告可能每次請求都記一筆** → 先接受，告警層去重；事件很小。
- **暫停同時計入輸出被擋** → 模型輸出偶爾被誤判會讓使用者較快被暫停。依規範的字面處理，並記為待決定：若使用者希望與 RPG 一致只計輸入，只需改一個查詢條件。
- **日界使用固定 UTC+8** → 與 `age_policy` 一致；若日後台灣採用日光節約時間才需要改為時區資料庫。
- **前端剩餘輪數與後端可能不同步** → 純顯示，真正的拒絕永遠在後端。

## Migration Plan

1. 備份後執行 `alembic upgrade head`（`0005` → `0006`）：只建立 `ix_llm_call_logs_created_at` 與 `ix_chat_messages_created_at`。舊版程式不受影響（多了索引而已）；新版程式在沒有索引時仍正確，只是較慢——符合「任何版本的程式碼都必須能在目前結構與下一版結構上運作」。
2. 部署新版程式。`USAGE_DAILY_TOKEN_LIMIT` 與 `USAGE_DAILY_CALL_LIMIT` 有預設值，不設也能啟動。
3. 回復：降級程式碼即可，索引可留著；若要移除，`alembic downgrade 0005_chat_core`。

## Open Questions

- 暫停是否也要計入「模型輸出被擋」（現為兩者都計，依 AGENTS.md 字面）。答案只影響一個查詢條件，不影響規格結構。
- 預設值（`USAGE_DAILY_TOKEN_LIMIT` 1,000,000、`USAGE_DAILY_CALL_LIMIT` 450）依目前模型的免費層配額；換模型或供應商時重設，不影響規格結構。
