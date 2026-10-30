# Design

## Context

- 帳號刪除目前只有一條路：`purge-inactive-accounts` 以條件式 `DELETE` 刪使用者，`user_identities`、`auth_tokens`、`chat_conversations`、`chat_messages` 靠資料庫 `ON DELETE CASCADE`，`llm_call_logs.user_id` 與 `admin_audit_logs.admin_user_id` 靠 `SET NULL`，再寫一筆 `deletion_tombstones`。`tests/architecture/test_user_foreign_keys.py` 保證每個指向 `users` 的外鍵不是 `CASCADE` 就是可為空的 `SET NULL`。
- **既有缺口**：`llm_call_logs.conversation_id` 刻意不設外鍵（SQL Server 錯誤 1785），模型註解寫明「帳號刪除的流程須先把編號清為空值再刪使用者」。使用者主動刪單一對話（`ChatRepository.delete_conversation`）有做，閒置清除沒有。
- `User.status` 已有 `deletion_pending` 值，但沒有任何寫入它的程式；`AuthService.login`、`resolve_session`、`GoogleSignInService`、`EmailFlowService` 都把它與 `suspended` 並列為「停用」而拒絕。要讓寬限期內能撤銷，這個並列必須拆開。
- 登入狀態是 Flask 簽章 cookie（`uid`、`tv`）；`require_login()` 與 `login_required_page` 是需要登入的端點的共同入口，`current_user()` 另被少數頁面控制器直接呼叫。
- 「提交成功後才寄信」已有機制（`database-access` 規格：延後動作），`AuthService.change_password` 即用它寄通知信。
- 對話頁、客服 API 與管理員後台的第一道閘門各自檢查帳號狀態（`AccessPolicy.chat_gate` 要求 `active`，`is_admin` 要求 `active`），所以即使受限登入狀態漏過了入口層，這些功能仍會被擋；本 change 的入口層預設拒絕是第二道防線，不是唯一一道。

動機與範圍見 proposal.md；各項行為見 specs。

## Goals / Non-Goals

**Goals:**
- 全站只有**一個**「抹除一個人」的出口。閒置清除、自助刪除，以及日後 RPG 的存檔與角色資料，都走它；新增指向使用者的資料表時，只需要改這一處與一個架構測試。
- 受限登入狀態**預設拒絕**，並由自動化測試掃描證明，而不是靠每個端點記得檢查。
- 匯出與清除都**記憶體有界**、可重複執行、輸出不含個資。

**Non-Goals:**
- 停權帳號的自助刪除：停權帳號無法登入，所以不可能申請（仍由 12 個月閒置清除處理）。
- 待刪除期間的「忘記密碼」：維持停用（`_BLOCKED_FROM_RESET`）。有密碼的帳號申請刪除時必須輸入密碼，寬限期內才忘記密碼的情形很少，且 Google 登入或等候期滿都能解決；不為它改動 `email-token-flows` 規格。
- 非同步匯出（排隊、寄下載連結）、匯出的簽章與加密、依使用者請求的部分匯出。
- 告警與監控（C-4d）、備份與還原（C-5）、日誌檔 30 天的保存（平台設定）。
- 刪除紀錄（`deletion_tombstones`）的清除：它不含個資，且備份還原的重新刪除需要它。

## Decisions

**D1　單一抹除出口：`AccountErasureService.erase`。**
輸入：一個「條件」（閒置或寬限期滿）、使用者編號、原因、現在時間。在**一個交易**內：①把 `llm_call_logs` 中該使用者的 `conversation_id` 清為空值（以 `user_id` 篩選，已有 `(user_id, created_at)` 索引）；②條件式 `DELETE users`（條件在刪除當下再確認一次，沿用既有做法）；③寫刪除紀錄；④提交。②的 `rowcount` 不是 1 時**回復整個交易**（把①也復原），因為使用者還活著，不能讓他的帳本失去對話連結。提交後才記錄事件。對話、訊息、身分、權杖靠串聯；帳本的 `user_id` 靠 `SET NULL`。順序不能反過來：使用者一刪，帳本的 `user_id` 就變空，再也找不到要清的列。
`AccountRetentionService.purge_inactive` 改為呼叫它，行為對外不變。替代方案「只在 Service 層迴圈刪對話」被否決：會繞過資料庫串聯、也留下可以漏掉某張表的空間；用串聯加上架構測試，漏掉的資料表會直接讓測試失敗。

**D2　`users` 增加兩個可為空的欄位（migration `0008`，expand）。**
`deletion_requested_at`（UTC）與 `status_before_deletion`（`Unicode(20)`）。後者只會是 `active` 或 `pending_verification`，撤銷時用它還原，避免「待驗證信箱的帳號藉由刪除再撤銷變成 `active`」。同一個 migration 為 `chat_conversations.updated_at` 增加索引 `ix_chat_conversations_updated_at`（期限清除要全表依最後活動時間篩選；既有索引以 `user_id` 開頭，用不上）。兩個欄位都不加約束、不進索引（待刪除的帳號極少，掃描即可）。舊程式不認識這兩個欄位也能運作；新程式在欄位尚未存在的資料庫上不會被部署（migration 先於程式，見營運文件）。
`status_before_deletion` 為空而狀態為 `deletion_pending`（理論上不會發生）時，撤銷依「信箱已驗證 → `active`，否則 `pending_verification`」還原；`deletion_requested_at` 為空時**永遠不視為到期**，保守地不自動刪除。

**D3　申請與撤銷都是單一條件式 UPDATE。**
申請：`WHERE id = :id AND role <> 'admin' AND status IN ('active','pending_verification')`，同時設 `status`、`deletion_requested_at`、`status_before_deletion` 並 `token_version = token_version + 1`（沿用 `bump_token_version` 的做法：不在 Python 讀取再寫回）。`rowcount` 為 0 時，依重新讀到的狀態回 403（管理員）或 409（已在待刪除）。撤銷：`WHERE id = :id AND status = 'deletion_pending' AND deletion_requested_at > :now - 7 天`，把 `status` 設回 `status_before_deletion`；`rowcount` 為 0 時，依狀態回 409 `grace_expired` 或 `not_pending`。與排程的刪除互相競爭時，兩邊各有條件式語句，資料庫只會讓其中一個成立。

**D4　確認本人：沿用 `confirm_password`，Google 帳號用固定文字。**
有密碼的帳號呼叫既有的 `AuthService.confirm_password`（與登入共用失敗計數與鎖定，與後台一致）。沒有密碼的帳號比對固定字串「刪除我的帳號」（NFC 正規化、去除前後空白）。端點依帳號是否有密碼決定要求哪個欄位，忽略另一個。替代方案「Google 帳號重新授權」被使用者否決（OAuth 流程要加 `prompt=login` 與 `auth_time` 驗證，工作量大）；這裡的風險由「7 天寬限＋撤銷＋通知信」承擔，見風險。

**D5　受限登入狀態：在入口層預設拒絕。**
- `resolve_session` 對 `deletion_pending` 改為回傳使用者（`suspended` 仍回 None）。`login` 與 Google 登入對 `deletion_pending` 不再拒絕，成功後 `status` 為 `deletion_pending`。
- `require_login(*, allow_pending_deletion=False)`：狀態為 `deletion_pending` 且未明確允許時拋出 `Forbidden`（403，代碼 `account_deletion_pending`）。`login_required_page` 同理，被拒絕時導向 `/account`。因此**所有現有端點不必修改**就自動拒絕；只有四個地方傳入 `allow_pending_deletion=True`：`/api/auth/me`、`/api/account/cancel-deletion`、登出（本來就不要求登入）與 `/account` 頁。
- 架構／整合測試：以受限登入狀態列舉 `app.url_map` 的每個規則，逐一請求（GET 與 POST），除允許清單外一律斷言 403 或導向 `/account`。新增端點若遺漏，測試不用改就會失敗或通過，行為由入口層保證。
- 否決方案「`current_user()` 對待刪除回傳 None」：呼叫端會把它當成未登入，得到 401 而不是有意義的 403，`/login` 也會誤判而讓人重複登入。

**D6　受限狀態的前端。**
`login.js` 與 Google 回呼在 `status === 'deletion_pending'` 時一律去 `/account`，不依 `next`。`/account` 的初始狀態含 `deletion_scheduled_at`（`/api/auth/me` 也含），頁面以 `v-if` 只渲染撤銷區塊。撤銷成功後重新載入頁面（以取得正常狀態與新的 CSRF 權杖）。Modal 一律是自製 Vue 元件，沿用既有的 `ui-components.js`。

**D7　通知信：`AccountMailer` 增加 `send_deletion_notice`。**
內容：已收到申請、預定刪除日、撤銷方式（登入後在帳號設定頁）、若非本人請立即登入撤銷並更改密碼。以延後動作在提交後寄出；寄信失敗只記錄、不影響結果（與其他信件相同）。信箱驗證關閉時不寄（沿用 `EMAIL_VERIFICATION_REQUIRED` 的語意）。

**D8　匯出：分頁讀取、寫入暫存檔、再回傳。**
`PersonalExportService.build(current, fmt)` 呼叫 `ChatRepository` 新增的 `export_conversations(user_id)` 與 `export_messages_page(user_id, after_id, limit)`（簽章第一個參數仍是 `user_id`，符合既有的架構測試）。訊息以主鍵為游標、每頁 500 筆，寫入 `tempfile.SpooledTemporaryFile`（超過 8 MB 才落地），因此記憶體與資料量無關。JSON 逐段寫出（不先組成大型物件）；CSV 以 `zipfile.ZipFile` 寫入同一個暫存檔，內含 `account.csv` 與 `messages.csv`，編碼 `utf-8-sig`。儲存格若以 `=`、`+`、`-`、`@`、Tab 或歸位字元開頭，前面加單引號。回應用 `send_file` 搭配 `Content-Disposition: attachment; filename="my-data-YYYYMMDD.json|zip"`（純 ASCII）與 `Cache-Control: no-store`。
否決方案：①全部組在記憶體——重度使用者的訊息可能很多，Cloud Run 的 `/tmp` 本身也算記憶體，所以分頁是必要的，暫存檔只是緩衝；②用 Flask 串流回應——Session 在回應傳完之前就已關閉，串流中途無法再讀資料庫。
匯出為 `POST /api/account/export`（本文 `{"format": "json" | "csv"}`），因此受 CSRF 與 Origin 檢查保護，也不會被預取連結觸發。

**D9　匯出內容的取捨。**
包含：帳號欄位、對話標題與摘要、訊息序號／角色／內容／時間。排除：密碼雜湊、`token_version`、權杖、Google 的 `provider_subject`、內部整數編號，以及 `intent`、`entities_json`、`suggested_actions_json`、`confidence`、`state_json` 這些模型的內部加工結果。排除後者的理由：它們不是使用者提供的資料，而是系統推論的副產品，且 `entities_json` 的內容形狀會隨模型調整；若日後認定須提供，擴充欄位即可，不影響格式相容。`llm_call_logs` 不含對話、不屬於使用者的個人內容（且可能已匿名化），不匯出。

**D10　期限清除：`DataRetentionService`，每類獨立、分批、各自提交。**
五個方法（對話、帳本、失敗原文、權杖、稽核）各自：以 `select(id) ... LIMIT :batch` 取一批編號（批次 500，也是 SQL Server 參數數量的安全範圍），在**一個交易**內處理這一批並提交，直到沒有為止。純 ORM，不寫原生 SQL。
- 對話：先把帳本中 `conversation_id IN (ids)` 清為空值，再 `DELETE chat_conversations WHERE id IN (ids)`（訊息串聯）。
- 帳本：`DELETE ... WHERE id IN (ids)`；失敗原文：`UPDATE ... SET error_detail = NULL WHERE id IN (ids)`（篩選 `error_detail IS NOT NULL AND created_at < 14 天前`，由既有的 `created_at` 索引縮小範圍）。
- 權杖：`expires_at < now - 7 天`；稽核：`created_at < now - 730 天`。
期限常數集中在 `app/services/data_retention_service.py`（對話與帳本 365 天、失敗原文 14 天、權杖 7 天、稽核 730 天，月以天計與閒置清除一致）；`/privacy` 的保存期限表與這些常數由測試比對，避免文件與實作脫節。
指令 `flask purge-expired-data [--dry-run]` 依序執行五類；某一類丟出資料庫錯誤時，回復該批、記錄類別名稱（不含內容）、繼續其餘類別，最後以非零代碼結束。預覽只做 `COUNT`。

**D11　排程指令與事件。**
`flask execute-account-deletions [--dry-run]`：找出 `status = 'deletion_pending' AND deletion_requested_at <= now - 7 天` 的編號（批次 200，只回編號），逐一走 D1。事件（`app/observability/lifecycle_events.py`，與 `admin_events.py` 同樣只接受固定列舉）：`account.deletion_requested`、`account.deletion_cancelled`、`account.deleted_by_request`、`account.exported`（附格式列舉）、`retention.purged`（附類別列舉與整數筆數）。已有的 `account.inactive_purged` 不變。

**D12　隱私權政策與條款版本。**
`privacy.html` 的資料權利段落改為描述實際行為，並把保存期限表標示為與指令一致。`CURRENT_TERMS_VERSION` 改為新的草稿版本字串。沒有「要求既有使用者重新同意」的機制，也不在本 change 新增：政策變更只是把已承諾的功能補齊，沒有新增蒐集用途。

## Risks / Trade-offs

- **被盜用的 session 可以申請刪除**（Google 帳號只需輸入固定文字）→ 緩解：7 天寬限、通知信（信箱驗證開啟時）、申請會使所有裝置登出；擁有者重新登入即可撤銷。接受這個風險，因為替代方案（重新授權）已被否決；寬限期的存在就是為了這種情況。
- **信箱驗證關閉的環境（development、testing、staging）沒有通知信** → 只影響非正式環境；正式環境必須開啟信箱驗證，啟動時已強制。
- **受限登入狀態是新的授權維度，漏網就是越權** → D5 的入口層預設拒絕＋全路由掃描測試；加上客服與後台各自的 `active` 檢查作為第二道防線。
- **匯出會長時間占用一個 worker** → 分頁讀取使記憶體有界，但時間與訊息數成正比。入口層限流（Nginx `limit_req`，C-5）承擔濫用；若實測發現大帳號超過 gunicorn 逾時，再改為非同步匯出（開放問題）。
- **清除與使用者同時操作** → 對話清除只動 12 個月沒有活動的對話；若剛好有新訊息，`updated_at` 已更新，對話不會被選中；批次內再以條件式語句確認。
- **雙軌差異**：`DELETE ... WHERE id IN (...)` 的參數數量（SQL Server 上限約 2100）以批次 500 避開；`SET NULL` 與 `CASCADE` 的路徑限制沿用既有 schema，不新增外鍵。需在真實 SQL Server 的測試中跑過完整抹除與清除。
- **期限過短造成資料遺失**：把「天」寫死為常數、以預覽模式先行檢視；第一次在正式環境執行前，必須先 `--dry-run` 並備份（營運文件）。
- **刪除不可逆**：寬限期是唯一的保險；備份還原時依刪除紀錄重新刪除（既有機制，原因欄多一個列舉值）。

## Migration Plan

1. 備份資料庫（既有流程：`BACKUP ... COPY_ONLY`，必須讀完結果集再確認檔案存在）。
2. `alembic upgrade head`：`0008` 新增兩個可為空欄位與一個索引，expand-only，新舊程式皆可運作。
3. 部署新版程式。
4. 平台排程器加入兩個指令（`execute-account-deletions` 每日一次、`purge-expired-data` 每日一次）；第一次手動以 `--dry-run` 檢視筆數。
5. 回復：程式可回到舊版（舊版不認識新欄位也能運作，但此時 `deletion_pending` 的帳號會被舊版拒絕登入而無法撤銷，且排程停止——寬限期因此被延長，不會提早刪除）；`downgrade` 會刪除這兩個欄位與索引，只在確定沒有待刪除帳號時使用。

## Open Questions

- 匯出的逾時與大小：等實際資料量與 gunicorn 設定（C-5）確定後，決定是否需要非同步匯出。
- `llm_call_logs` 是否在匯出中提供「用量摘要」（次數、token）：目前不提供；個資法的「複製」範圍若經法務確認包含，再補欄位。
