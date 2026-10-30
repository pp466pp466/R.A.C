# Tasks

## 1. 資料模型與 migration

- [x] 1.1 `User` 新增可為空的 `deletion_requested_at`（UTC）與 `status_before_deletion`（`Unicode(20)`）；`DeletionTombstone` 增加原因常數 `REASON_USER_REQUESTED = "user_requested"` 並加入 `DELETION_REASONS`；`ChatConversation` 新增索引 `ix_chat_conversations_updated_at`。migration `0008`（`render_as_batch`、純 Alembic op API、expand-only：兩欄位可為空、不加約束）。驗證：`tests/test_migrations.py` 新增 0008 的升降級（只多兩欄一索引、舊資料不變）、「遷移後的結構與模型無差異」在 SQLite 與 SQL Server 皆通過，`tests/architecture/test_user_foreign_keys.py` 通過，既有的模型測試（`USER_STATUSES` 等）不變

## 2. 單一抹除出口

- [x] 2.1 `UserRepository` 新增：`clear_ledger_conversation_refs(user_id)`（把該使用者的 `llm_call_logs.conversation_id` 清為空值，單一 UPDATE）、`delete_if_pending_due(user_id, cutoff)`（條件式 DELETE：狀態為 `deletion_pending` 且 `deletion_requested_at <= cutoff`，空值視為未到期）、`find_pending_due_ids(cutoff, limit)`、`count_pending_due(cutoff)`。驗證：`tests/test_user_repository_erasure.py` 涵蓋剛好到期與差一秒、空的申請時間不算到期、狀態不符不刪、串聯刪除對話／訊息／身分／權杖、帳本 `user_id` 與 `conversation_id` 皆為空值而其他使用者的紀錄不變，SQLite 與 SQL Server 皆通過
- [x] 2.2 新增 `app/services/account_erasure_service.py`：`erase(condition, user_id, reason, now)` 在一個交易內「清帳本對話參照 → 條件式刪除 → 寫刪除紀錄 → 提交」，刪除的 `rowcount` 不是 1 時回復整個交易；提交後才記錄事件。`AccountRetentionService.purge_inactive` 改為呼叫它。驗證：`tests/test_account_erasure_service.py` 涵蓋條件在刪除當下不成立時帳本參照完全不變（交易回復）、編號重用後舊帳本不指向新對話、刪除紀錄只有編號時間與原因、事件不含個資；`tests/test_account_retention_service.py` 原有測試全數仍通過，並新增「閒置帳號的帳本被匿名化」；新增架構測試：每張指向 `users` 的資料表與每張指向 `chat_conversations` 的資料表，都在抹除流程涵蓋的清單內（新增資料表漏掉就失敗），並自我驗證

## 3. 受限登入狀態（預設拒絕）

- [x] 3.1 `AuthService.resolve_session` 對 `deletion_pending` 回傳使用者（`suspended` 仍回 None）；`login` 與 `GoogleSignInService` 對 `deletion_pending` 不再拒絕，只拒絕 `suspended`；`CurrentUser`／`public_dict` 在待刪除時帶 `deletion_scheduled_at`（申請時間加 7 天，UTC ISO）。驗證：`tests/test_auth_login.py`、`tests/test_auth_session_service.py`、`tests/test_google_sign_in_service.py` 中原本參數化「`suspended` 與 `deletion_pending`」的案例拆開：停權仍被拒絕，待刪除登入成功且狀態為 `deletion_pending`、密碼錯誤的待刪除帳號仍回通用失敗並計次；`/api/auth/me` 的回應含 `deletion_scheduled_at` 且仍不含生日與內部欄位
- [x] 3.2 `require_login(*, allow_pending_deletion=False)`：待刪除且未允許時拋出 `Forbidden`（代碼 `account_deletion_pending`）；`login_required_page` 對受限狀態導向 `/account`（`/account` 本身允許）；`/api/auth/me` 允許。`login.js` 與 Google 回呼在待刪除時一律前往 `/account`。驗證：`tests/test_restricted_session.py` 以受限登入狀態逐一請求 `app.url_map` 的每個規則，除允許清單（`/api/auth/me`、`/api/account/cancel-deletion`、`/api/auth/logout`、`/account`、公開頁與靜態檔）外全部回 403 `account_deletion_pending` 或導向 `/account`，並自我驗證（臨時加一個未允許的端點必須被掃描偵測到）；客服 API、`/cs`、`/admin` 與 `/api/admin/*` 各有明確案例

## 4. 申請與撤銷刪除

- [x] 4.1 `UserRepository` 新增 `request_deletion(user, now)`（條件式 UPDATE：非管理員且狀態為 `active`／`pending_verification`；同時設狀態、申請時間、申請前狀態並使 `token_version` 加 1）與 `cancel_deletion(user, now)`（條件式 UPDATE：狀態為 `deletion_pending` 且申請未滿 7 天，還原申請前的狀態並清除兩欄；申請前狀態為空時依信箱是否已驗證還原）。驗證：`tests/test_user_repository_deletion_request.py` 涵蓋每個前置條件、剛好 7 天與差一秒、並行申請只有一次成功（`concurrent_engine`）、待驗證帳號撤銷後仍待驗證、`token_version` 確實加 1，SQLite 與 SQL Server 皆通過
- [x] 4.2 新增 `app/services/account_deletion_service.py`：`request(current, password=None, confirm_text=None)`（有密碼者走 `AuthService.confirm_password`，沒有密碼者比對「刪除我的帳號」；管理員 `Forbidden` 代碼 `admin_delete_forbidden`；已在待刪除回 409）與 `cancel(current)`（逾期回 409 `grace_expired`、不在待刪除回 409 `not_pending`）；`AccountMailer` 新增 `send_deletion_notice`，信箱驗證開啟時於提交後寄出繁體中文通知信，寄信失敗不影響結果；新增 `app/observability/lifecycle_events.py`（固定列舉、列舉外的值立刻出錯）。驗證：`tests/test_account_deletion_service.py` 涵蓋四種確認組合、全形空白與 NFC 的確認文字、錯誤密碼計入失敗並可鎖定、管理員、重複申請、撤銷的各種邊界、通知信的收件人與內容（含預定刪除日）、信箱驗證關閉時不寄信、事件不含個資
- [x] 4.3 `app/controllers/account_api_controller.py`：`POST /api/account/delete`（成功後清除目前裝置的登入狀態並回傳說明）、`POST /api/account/cancel-deletion`（允許受限狀態）；註冊藍圖、`/api/account/` 加入 no-store 前綴。驗證：`tests/test_account_api.py`：未登入 401、沒有 CSRF 權杖 403 且不改變資料、成功後原本的登入狀態失效、重新登入得到受限狀態且只能撤銷、撤銷後完整功能恢復、請求夾帶 `role`／`status` 等欄位被忽略、回應含 no-store

## 5. 排程：執行刪除

- [x] 5.1 `flask execute-account-deletions [--dry-run]`：找出寬限期滿的帳號（批次 200，只取編號），逐一走抹除出口，原因 `user_requested`；預覽只輸出筆數；`registry.py` 組裝 `build_account_deletion_executor`。驗證：`tests/test_cli_execute_account_deletions.py` 涵蓋預覽不改任何資料、期滿刪除並留下刪除紀錄、未滿 7 天不刪、重複執行第二次為 0、輸出與日誌不含 email、名稱、生日；`tests/test_cli.py`（或既有的指令清單測試）涵蓋新指令已註冊

## 6. 匯出個人資料

- [x] 6.1 `ChatRepository` 新增 `export_conversations(user_id)`；訊息沿用既有的 `messages_after(user_id, 對話, after_seq, limit)`（序號游標、已帶擁有者條件），不另加重複的方法。驗證：`tests/test_chat_repository_export.py` 涵蓋只回傳擁有者的資料、頁邊界、排序（序號）、空資料、別人的對話讀不到訊息，並確認既有的「Repository 簽章須帶 `user_id`」架構測試通過
- [x] 6.2 新增 `app/services/personal_export_service.py`：JSON（UTF-8 無 BOM、`ensure_ascii=False`）與 CSV（zip 內 `account.csv`、`messages.csv`，`utf-8-sig`）；以 `SpooledTemporaryFile` 分頁寫入；公式字元（`=`、`+`、`-`、`@`、Tab、歸位字元）開頭的儲存格前加單引號；排除憑證、內部編號與模型內部欄位。驗證：`tests/test_personal_export_service.py` 涵蓋繁體中文與表情符號往返、含逗號雙引號換行的 CSV、公式字元被中和、欄位白名單（找不到雜湊、權杖、`provider_subject`、內部編號、`intent`／`entities`）、超過一頁（> 500 則）的資料完整且有序、跨使用者隔離、BOM 檢查
- [x] 6.3 `POST /api/account/export`（本文 `format`：`json` 或 `csv`，其餘值 400）：回傳附件與 `Cache-Control: no-store`，檔名純 ASCII，成功後記錄 `account.exported`；受限狀態 403。驗證：`tests/test_account_export_api.py`：未登入 401、沒有 CSRF 403、格式錯誤 400、夾帶其他使用者識別碼被忽略、待刪除 403、回應標頭、日誌只有事件名稱／內部編號／格式，不含生日與內容

## 7. 期限清除

- [x] 7.1 `ChatRepository`／`LlmCallLogRepository`／`AuthTokenRepository`／`AdminAuditRepository`／`DeletionTombstoneRepository` 不新增「依 ID 取他人資料」的方法；新增專用的清除用 Repository `app/repositories/retention_repository.py`：每類 `find_expired_ids(cutoff, limit)`／`count_expired(cutoff)`／`delete_ids(ids)`（對話先清帳本參照；失敗原文為 `clear_error_detail(ids)`）。驗證：`tests/test_retention_repository.py` 涵蓋每類剛好過期與差一秒、`updated_at` 較新的舊對話不選中、對話清除後帳本保留且參照為空、失敗原文清空而列保留、批次上限，SQLite 與 SQL Server 皆通過；架構測試確認它只被 `data_retention_service.py` 與 `registry.py` 匯入，且 `AdminAuditRepository` 仍沒有更新與刪除的公開方法
- [x] 7.2 新增 `app/services/data_retention_service.py`：期限常數（對話與帳本 365 天、失敗原文 14 天、權杖 7 天、稽核 730 天）、每類獨立分批（500）各自提交、預覽模式只計數、某類失敗時回復該批並繼續其餘類別、記錄 `retention.purged`（類別與筆數）。驗證：`tests/test_data_retention_service.py` 涵蓋五類各自的邊界、刪除紀錄不被清除、近期紀錄與今日額度不受影響（`UsageService` 的今日數字前後相同）、一類失敗不阻止其餘、超過一批的資料全部清除、第二次執行為 0、事件與輸出只有筆數
- [x] 7.3 `flask purge-expired-data [--dry-run]`：輸出各類筆數，有任一類失敗時以非零代碼結束並標明類別；`registry.py` 組裝。驗證：`tests/test_cli_purge_expired_data.py`：預覽、重複執行、失敗類別的結束代碼、輸出與日誌不含任何內容；指令清單測試涵蓋新指令

## 8. 前端

- [x] 8.1 `app/static/js/auth/account-lifecycle.js`（純函式：確認文字常數與比對、匯出檔名與 MIME 的處理、預定刪除時間的台灣時間顯示、刪除按鈕的啟用條件、錯誤代碼對應的繁體中文說明），不依賴 DOM。驗證：`tests/js/account-lifecycle.test.js`（確認文字的全形空白與 NFC、`grace_expired`／`account_deletion_pending`／`admin_delete_forbidden` 各有說明、未知代碼的退路）
- [x] 8.2 `account.html` 與 `account.js`：匯出區塊（JSON、CSV 兩鈕，axios `blob` 下載、`AbortController`、逾時 60 秒、`finally` 恢復按鈕）、刪除帳號區塊（自製 Modal：說明 7 天寬限與範圍並建議先匯出，有密碼者輸入密碼、其餘輸入確認文字，確認前不送出，送出或取消後清空欄位）、管理員不顯示刪除、待刪除時只渲染預定時間／撤銷／登出。沿用 `v-if`／`v-show`、不使用 Bootstrap 的 JavaScript 與 `v-html`，色彩符合規範。驗證：`node --test tests/js` 通過、`tests/architecture/test_frontend_rules.py` 通過、`tests/test_account_page.py` 涵蓋初始狀態（待刪除者的頁面不含變更密碼等區塊、管理員無刪除區塊、初始狀態不含生日）

## 9. 隱私權政策與條款版本

- [x] 9.1 `privacy.html` 的資料權利段落改為描述實際行為（匯出內容與格式、刪除帳號流程與撤銷、抹除範圍、帳本匿名化），移除「將於後續版本提供」；保存期限表的數值與期限常數一致；`legal.py` 的 `CURRENT_TERMS_VERSION` 更新。驗證：`tests/test_legal_pages.py` 斷言頁面不再含「後續版本提供」、保存期限表出現與 `data_retention_service` 常數一致的天數與月數、版本字串已更新；`tests/test_readme.py`／`tests/test_design_docs.py` 不因此失敗

## 10. 整合、升級、驗收與文件

- [x] 10.1 端到端整合：以假供應商與假寄信器走完「註冊登入 → 對話 → 匯出 → 申請刪除（所有裝置登出）→ 重新登入只能撤銷 → 撤銷 → 再申請 → 時間推進 7 天 → 排程刪除 → 帳本匿名化、刪除紀錄、同信箱可重新註冊」，與「資料推進 12 個月後 `purge-expired-data`」。驗證：`tests/test_privacy_lifecycle_flow.py` 通過，並以日誌與指令輸出掃描證明整段流程沒有 email、名稱、生日、密碼、確認文字、對話與失敗原文
- [x] 10.2 本機開發資料庫 `project`：備份（`COPY_ONLY, CHECKSUM`，`RESTORE VERIFYONLY` 通過；備份必須讀完所有結果集再確認檔案存在）後 `alembic upgrade head` 到 `0008`，確認兩欄與索引、既有資料筆數不變、`/readyz` 回 200。驗證：`alembic current` 為 `0008`，欄位皆可為空
- [x] 10.3 瀏覽器驗收（Edge，桌面與 360px）：登入 → 匯出 JSON 與 CSV（檢視下載內容與中文）→ 刪除帳號 Modal（密碼錯誤、成功）→ 被導向登入 → 重新登入只見撤銷區塊（嘗試開啟 `/cs`、`/admin` 被導回 `/account`）→ 撤銷 → 完整頁面恢復；Google 帳號的確認文字路徑以假的身分提供者走一次；逐張檢視截圖。驗證：驗收腳本全數通過、截圖已檢視、缺陷已修正
- [x] 10.4 全部測試通過（`pytest`、`node --test tests/js`）；README 更新（`/api/account/*` 端點表、新增兩個 flask 指令、刪除與匯出章節與已知限制、驗收紀錄）。驗證：`pytest tests/test_readme.py` 通過，端點表與實際註冊的路由完全一致
- [x] 10.5 更新 docs/ 對應的設計文件（對照表見 docs/README.md）：`資料模型-data-model.md`（兩個新欄位、索引、migration `0008`、保存期限圖改為已實作）、`帳號與安全-auth-security.md`（狀態圖的 `deletion_pending`、受限登入狀態與預設拒絕、申請與撤銷）、`系統架構-architecture.md`（抹除出口與清除用 Repository 的分層規則）、`營運與部署-operations.md`（兩個排程指令、保存期限表的狀態、事件）、`前端結構-frontend.md`（帳號頁的新模組）、`模型呼叫層-llm-layer.md`（帳本期限清除已實作）、`路線圖-roadmap.md`（C-4c 狀態、新增 C-4d `ops-alerting`）；並讓 `tests/test_design_docs.py` 通過

## Workflow follow-up

- 實作完成後以 `/openspec-archive-change privacy-lifecycle` 同步主規格並封存（新增 `account-deletion`、`personal-data-export`、`data-retention`，補充 `account-retention`、`session-authentication`、`google-sign-in`、`access-control`、`auth-pages`、`service-observability`）。
- C-4d `ops-alerting` 另立 change（告警管道、5xx、延遲、模型錯誤、登入失敗激增、寄信失敗、`usage.global_budget_warning` 去重）。
