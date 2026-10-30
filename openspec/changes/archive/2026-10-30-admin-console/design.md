# Design

## Context

- 管理員目前只有「建立」這一件事：`flask create-admin` 建立角色 `admin`、狀態 `active`、信箱視為已驗證、有密碼的帳號；`AccessPolicy.is_admin` 已存在，但沒有任何端點使用它。
- 停權帳號的既有行為已經完整：`resolve_session` 對 `suspended` 回 None（登入狀態立刻失效）、登入回 403 `account_disabled`、忘記密碼與 Google 登入都擋停用狀態。後台只需要「把狀態改成 `suspended`」並讓 `token_version` 加 1。
- 密碼驗證與失敗鎖定在 `AuthService`（`_count_failure`：連續 5 次鎖定 15 分鐘，計數在資料庫）；`change_password` 已有「驗證目前密碼」的邏輯，但綁在變更密碼的流程裡。
- 對話資料的存取受「Repository 方法第一個參數必須是 `user_id`」的架構測試約束（`ChatRepository`）。後台要讀他人的對話，**不能**放寬這條規則，必須是另一組明確標示為後台專用的方法。
- 請求的 Session 只在狀態碼小於 400 時提交；需要在失敗回應後仍保留的寫入，明確呼叫 `keep_changes_on_error()`。
- SQL Server 不允許同一張表有兩個指向同一個父表且帶動作（`CASCADE`／`SET NULL`）的外鍵（錯誤 1785，`llm_call_logs.conversation_id` 已遇過同類問題）。
- 帳本 `llm_call_logs` 有 `conversation_id`（無外鍵）、`status = blocked`、`error_detail`；`chat_messages.intent` 有索引；這些就是「被標記」與「意見回饋」的資料來源，不需要新增欄位。

見 proposal.md 的動機與範圍；用量計算沿用 C-4a 的 `quota_policy.taipei_day_bounds`。

## Goals / Non-Goals

**Goals:**
- 管理員能處理三件事：看全站狀況、處理單一使用者（停權、解除、調整等級）、處理被標記或回饋的對話。
- **可讀範圍最小**：清單沒有訊息內容；只有兩類對話可讀全文；使用者資料沒有任何個別的年齡資料（生日、年齡區間、年齡群組），年齡只有總覽的區間人數彙總。
- **每個操作可追溯**，且稽核與動作同一交易；危險操作要重新輸入密碼。
- 「只有管理員」與「後台資料只走後台路徑」由測試守住，不靠程式碼審查。

**Non-Goals:**
- 不做 RPG 的性格原型審核（RC-5）、`error_detail` 與稽核的保存期限清除（C-4c）、刪除帳號與匯出（C-4c）、告警（C-4c）。
- 不做額度數值的後台調整、角色變更、來源 IP 限制、雙因素驗證、寄信通知被停權者。
- 不提供「解除客服對話暫停」：它由帳本推得，1 小時自然到期（C-4a）。

## Decisions

**D1　`AccessPolicy.require_admin`：唯一入口，非管理員 404。**
沿用既有的 `is_admin`（`admin` 且 `active`），新增 `require_admin(user)`：不是就記錄 `admin.access_denied`（只含內部編號）並拋出 `NotFound`。與 AGENTS.md「非擁有者一律 404、避免洩漏資源是否存在」同一個原則，所以 `/admin` 對一般使用者就像不存在。頁面端：未登入先走 `login_required_page`（導向登入），已登入再檢查管理員。替代方案「403」被否決：它會告訴一般使用者後台存在。

**D2　一個 `AdminService`，每個公開方法第一行是 `require_admin`。**
所有後台能力集中在 `app/services/admin_service.py`，控制器只呼叫它。以 AST 架構測試守住兩件事：①每個公開方法的第一個陳述式是 `self._policy.require_admin(admin)`；②後台專用的 Repository（`admin_repository.py`、`admin_audit_repository.py`）只被 `admin_service.py` 與 `registry.py` 匯入。這與 `ChatRepository` 的「第一個參數必須是 `user_id`」測試對稱：一般路徑強制帶擁有者，後台路徑強制帶管理員檢查。

**D3　兩個後台專用的 Repository，與既有的分開。**
`AdminRepository`：以對外識別碼或信箱取得使用者、條件式地改狀態與等級、各種彙總、意見回饋與被標記的查詢、對話的讀取與刪除、失敗原文。`AdminAuditRepository`：只有 `add` 與 `list`，**沒有更新與刪除**（測試以反射檢查公開方法）。不改動 `ChatRepository` 與 `UserRepository`，所以既有的隔離保證完全不受影響。

**D4　`admin_audit_logs` 資料表（migration `0007`）。**
欄位：`id`、`admin_user_id`（外鍵 `users.id`、`SET NULL`、可為空）、`action`（`Unicode(40)`）、`target_user_public_id`、`target_conversation_public_id`（`Unicode(36)`，可為空，**不設外鍵**）、`reason`（`Unicode(200)`，可為空）、`detail_json`（JSON，只放列舉與整數）、`created_at`。索引：`(created_at)`、`(admin_user_id, created_at)`。**只有 `admin_user_id` 有外鍵**：兩個指向 `users` 的 `SET NULL` 會被 SQL Server 拒絕；目標存對外識別碼（UUID，不是個資）也讓紀錄在使用者被刪除之後仍然成立，這正是稽核該有的性質。動作列舉定義在模型模組，`AdminAuditRepository.add` 在寫入前驗證。

**D5　稽核與動作同一交易；失敗的重新驗證另外保存。**
後台 Service 與請求共用同一個 Session：改狀態、改等級、刪除對話與它們的稽核都在請求的交易內，請求成功才一起提交，例外就一起回復。**讀取類**（查使用者、讀對話、看失敗原文）先 `add` 並 `flush` 稽核，再讀取資料；稽核寫入失敗就不會回傳內容。重新驗證失敗是 400，請求交易不會提交，所以 `AdminService` 注入 `persist_on_error`（即 `keep_changes_on_error`），與 `AuthService` 保存失敗計數同一個機制。

**D6　重新輸入密碼：`AuthService.confirm_password`，AdminService 注入它。**
新增公開方法 `confirm_password(current, password)`：沒有密碼雜湊 → `ValidationError(code="no_password")`；帳號被鎖定或雜湊不符 → 計入失敗（沿用 `_count_failure`，與登入共用計數與鎖定）並拋出 `ValidationError(fields={"password": …})`；成功**不**重設失敗計數（只有登入成功才重設）。`AdminService` 只接收一個可呼叫物件，不認識雜湊器，測試時可替換。四個危險操作（停權、解除、調整等級、刪除對話）一律先呼叫它；失敗時寫 `reauth.failed`（細節是嘗試的動作）與日誌事件，再把錯誤往外傳。

**D7　對使用者的操作：條件式 UPDATE，目標只能是一般使用者。**
停權 `UPDATE users SET status='suspended', token_version=token_version+1 WHERE id=? AND status IN ('active','pending_verification')`，rowcount 為 0 就回 409；解除 `WHERE status='suspended'`，結果狀態由「信箱驗證是否開啟且 `email_verified_at` 為空」決定（`AdminService` 取得與 `AccessPolicy` 相同的設定值）；等級 `WHERE tier <> :new`。條件式 UPDATE 讓兩位管理員同時操作也不會互相覆蓋。目標必須 `role='user'`（管理員一律 403 `admin_target_forbidden`，含自己），`deletion_pending` 回 409。

**D8　找使用者：警告是主要入口，信箱精確查詢是次要入口，沒有瀏覽路徑。**
管理員平常只透過違反條例的警告找使用者：總覽的「今日警告」到被標記清單，每一筆帶該使用者今日被擋次數與是否暫停中（D9），由此進入對話與使用者詳情，決定刪除內容或停權。**以信箱查詢只用在客服反應的情形**（例如使用者來信要求升級），介面標明用途，稽核的細節註明途徑（信箱或識別碼），管理員因此看得出哪些查詢是例外入口。`POST /api/admin/users/lookup`，信箱放在本文（網址會進入日誌與瀏覽器歷史，雖然日誌只記路徑）；以註冊相同的 `normalize_email` 與 `canonical_email` 比對，所以 Gmail 的別名寫法也找得到。**不提供**使用者清單、模糊搜尋與依其他欄位搜尋——這是 AGENTS.md「預設只能看統計與兩類對話」在「需要找到人才能停權或改等級」這個必要性下的最小讓步：管理員必須先知道信箱（例如使用者來信要求升級），或從被標記清單進入。每次查詢都寫稽核，稽核裡只有查到的對外識別碼。替代方案「提供搜尋清單」被否決：它會讓管理員能瀏覽全部使用者的 email。

**D9　對話的可讀範圍：兩類，且用同一份查詢定義。**
- **意見回饋**：`chat_messages` 中 `role='assistant' AND intent IN ('complaint_feedback', 'human_handoff')`，依對話分組，取數量與最近時間。
- **被標記**：`llm_call_logs` 中 `kind='chat' AND status='blocked' AND conversation_id IS NOT NULL`，依對話分組，與 `chat_conversations` 連接（對話已被刪除就自然消失）。
- 讀取與刪除的資格判斷用同樣的兩個條件（兩個 `EXISTS`），所以「清單裡有的才讀得到」不會有第二套規則。不符資格與不存在得到同一個 404。
- **為什麼包含 `human_handoff`、不包含 `unknown`**（使用者決定，2026-10-29）：客服的提示詞在三種情況會告訴使用者「管理員會看到」——`complaint_feedback`、`human_handoff`、`unknown`。只算第一種時，使用者要求真人客服後說出的問題若被歸為 `unknown`（例如 C-6 之前不在範圍的訂單），管理員讀不到，卻已被承諾過兩次。`human_handoff` 是使用者主動要求有人處理的明確訊號，加入它補上這個缺口、範圍只多一個「使用者主動要求」的條件。`unknown` 不加入：它還包含離題閒聊與「忽略以上指示」之類的越權試探，全部開放會違反「不得任意讀取其他對話全文」。代價：使用者沒有要求真人、只直接問一個被歸為 `unknown` 的問題時，管理員仍讀不到；這是刻意接受的。
- **警告的輕重**：被標記清單每一筆另帶該使用者「今日被擋的對話呼叫總次數」與「客服對話暫停的截止時間」。這兩個值由同一次分組查詢（本頁使用者的 `user_id IN (…)`、今日、`kind=chat`、`status=blocked`，取次數與最近時間）加上與 `UsageService` 完全相同的規則算出；為避免兩處各寫一份，把「次數與最近時間 到 暫停截止時間」抽成 `quota_policy` 裡的一個純函式，`UsageService.suspended_until` 與後台共用，並以測試守住兩者一致。
- 清單只有中繼資料；全文只在讀取端點，最多 200 則（`has_more` 告知還有更多）。回傳不含使用者的 email 與顯示名稱，只有對外識別碼。

**D10　刪除整個對話，不刪單則。**
單則刪除會破壞序號的連續性、輪的配對與摘要的涵蓋範圍。`AdminRepository.delete_conversation` 在同一個交易先把帳本的 `conversation_id` 清為空值（與使用者自己刪除時相同，否則 SQLite 重用編號會錯指別人的新對話），再刪對話（訊息靠 `ON DELETE CASCADE`）。帳本保留（用量與成本稽核不能因為刪內容而少），所以該對話離開被標記清單。

**D11　失敗原文：固定 14 天視窗，查詢本身就限制。**
`GET /api/admin/failures`：`status='validation_failed' AND error_detail IS NOT NULL AND created_at >= now - 14 天`，最多 100 筆。實際的清除排程是 C-4c，但**讀取端不依賴排程**：即使清除還沒做，超過 14 天的原文也讀不到。

**D12　總覽：每個台灣日一個彙總查詢；今日警告三個計數。**
`AdminRepository.usage_for_day(start, end)` 以條件式加總（`SUM(CASE WHEN … THEN 1 ELSE 0 END)`）一次取得 token、呼叫次數、計入額度的輪數與被擋次數；近 7 日就是 7 次查詢（後台很少被打開，不另外做快取）。日界沿用 `taipei_day_bounds`。使用者人數與等級以 `GROUP BY` 計。年齡彙總沿用 `AgeStatsService`。全站比例 = 今日用量 ÷ `USAGE_DAILY_*`。今日警告：一次分組查詢取得每位使用者今日被擋的次數與最近時間，**被擋的呼叫數**是次數的總和、**被擋的使用者數**是列數、**暫停中的使用者數**是套用上述純函式後仍在暫停期間的人數（使用者人數以「今日有被擋紀錄的人」為界，不會掃全部使用者）。

**D13　控制器與頁面。**
`admin_api_controller.py`（`/api/admin`，12 個端點）與 `admin_page_controller.py`（`/admin`）；`app/security/cache_headers.py` 把 `/api/admin/` 加入 no-store 前綴。分頁參數 `limit`（1 至 100，預設 50）與 `offset` 的解析放在 `request_helpers.py` 的共用函式（目前客服控制器有同樣的私有版本，本 change 不重構它）。

**D14　前端：與客服頁面相同的規範，三個檔案。**
`templates/admin/console.html`（Vue 範本放在 raw 區塊）、`static/js/admin/admin-format.js`（無框架的純函式：日期以台灣時間顯示、狀態與等級與動作的中文標籤、用量比例的百分比與等級顏色；Node 可直接測試）、`static/js/admin/admin.js`（Vue 應用）。六個分頁：總覽、被標記對話、意見回饋、使用者、模型失敗、稽核（依管理員的使用順序）。**總覽最上面是「今日警告」卡片，點它切到被標記對話**；被標記對話的每一列有「看對話」與「看使用者」；「使用者」分頁只有以信箱查詢的區塊，標題標明「供客服反應時使用」，沒有使用者清單。危險操作共用一個「確認」Modal（原因欄位在需要時顯示、密碼欄位 `type=password` 且 `autocomplete="off"`，送出或取消後立即清空，不存進任何儲存體）；對話全文在另一個 Modal 以純文字顯示。不引入新的相依套件。

**D15　日誌事件。**
`app/observability/admin_events.py`：`admin.access_denied`、`admin.reauth_failed`、`admin.action`；原因是固定列舉（動作名稱或 `not_admin`），傳入列舉以外的值立刻出錯，與 `usage_events` 同一個做法。不需要新的白名單欄位（`event`、`user_id`、`reason` 都已存在）。

## Risks / Trade-offs

- **被盜用的管理員登入狀態可以讀被標記與意見回饋的對話、查詢使用者** → 這些讀取不需要重新輸入密碼（AGENTS.md 只要求高風險操作），但每一次都有稽核，且寫入類操作仍需要密碼；管理員的登入狀態與其他使用者一樣 7 天到期、改密碼即失效。若之後要加強，可讓「讀全文」也要求密碼（只改一個呼叫）。
- **「意見回饋」依賴模型對意圖的分類** → 誤判會讓某些一般對話被當成回饋而可被讀取，或漏掉某些回饋。這是用模型分類的固有限制；可讀範圍仍被「助理訊息的意圖」這個資料事實限制，不是管理員自行選擇，且每次讀取有稽核。
- **管理員輸入錯誤密碼也會累積登入的失敗計數** → 連續 5 次會把自己鎖定 15 分鐘（唯一的管理員會暫時進不來）。這是刻意的：它讓後台不會成為暴力猜測密碼的捷徑。
- **原因欄位是自由文字，管理員可能打進個資** → 介面標示「請勿填寫個人資料」，長度限制 200 字；保存期限與稽核相同（24 個月，清除是 C-4c）。
- **稽核成長**：每次查詢與讀取都新增一列 → 欄位很小、有 `created_at` 索引；保存期限的清除由 C-4c。
- **被標記清單依賴帳本的 `conversation_id`** → 使用者自己刪除對話時它被清空，該對話離開清單；內容已被使用者刪除，這是預期的。
- **總覽 7 個彙總查詢** → 只在打開總覽時執行；若日後有負擔再改為資料庫中的每日彙總列（另案）。

## Migration Plan

1. 備份後執行 `alembic upgrade head`（`0006` 到 `0007`）：只建立 `admin_audit_logs` 與兩個索引。舊版程式不認識這張表也不受影響，所以先升級資料庫再部署新版程式是安全的。
2. 部署新版程式。沒有新的環境變數。首位管理員仍以 `flask create-admin` 建立（已存在）。
3. 回復：降級程式碼即可；`alembic downgrade 0006_usage_indexes` 會刪除 `admin_audit_logs` 與其中的全部紀錄（已有真實稽核之後不可使用，請改用備份還原）。
