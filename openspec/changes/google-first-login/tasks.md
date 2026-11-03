# Tasks

> 本 change 只動玩家的登入體驗、年齡確認、登入有效期、帳號鎖定與申訴、管理員二階段登入。**不移除**玩家端的密碼註冊／登入 API 與信箱驗證（change ②）。每組做完都要跑該組新增的測試與 `pytest` 中受影響的既有測試；既有測試中假設「註冊時填生日」「登入後進 `/account`」「管理員以密碼登入」的部分，在對應的任務裡改寫，不留失敗。

## 1. 資料模型與 migration

- [X] 1.1 `app/models/user.py` 新增 `age_deadline_at`（可為空、索引）、`locked_at`（可為空、索引）、`lock_reason`（可為空）與鎖定原因常數；新增 `app/models/lock_strike.py`（`lock_strikes`）與 `app/models/lock_appeal.py`（`lock_appeals`，含 `public_id`），註冊進 `app/models/__init__.py`。驗證：`tests/test_models_*` 風格的新測試確認欄位型別為 Unicode／UTCDateTime、外鍵為 `ON DELETE CASCADE`、索引存在
- [X] 1.2 新增 Alembic migration（expand：只新增可為空的欄位與兩張新表，不含原生 SQL）。驗證：`tests/test_migrations.py` 在 SQLite 升到最新並可降回；SQL Server 整合測試（`project_test`）升級成功；`tests/architecture/test_user_foreign_keys.py`、`test_account_erasure_coverage.py` 納入兩張新表後通過
- [X] 1.3 `UserRepository` 新增：玩家帳號的條件式刪除（角色非管理員且沒有生日）、逾時帳號的 id 查詢與計數（`age_deadline_at` 到期且沒有生日、角色 `user`）、`count_locked(now)` 合計密碼鎖定與帳號鎖定、鎖定與解除的條件式 UPDATE；新增 `LockStrikeRepository`（寫入、視窗內計數、清除舊紀錄、清除某帳號的計次）與 `LockAppealRepository`（寫入、最近一小時筆數、是否有待處理、列出被鎖定帳號的申訴、標為已處理）。驗證：新增 `tests/test_user_repository_lock.py`、`test_lock_strike_repository.py`、`test_lock_appeal_repository.py`（SQLite 與 SQL Server 各跑一次）——條件式 UPDATE 並行只成功一次、視窗邊界、申訴只回傳被鎖定帳號的、沒有生日的管理員不會被選出

## 2. 登入有效期與受限登入狀態

- [X] 2.1 `app/security/session.py` 以 `SlidingSessionInterface` 取代 `AbsoluteExpirySessionInterface`（D1）；`PERMANENT_SESSION_LIFETIME` 改為 30 天（`app/security/cookies.py`）。驗證：改寫 `tests/test_session_security.py`、`test_security_cookies.py`——內容仍只有 `uid`、`tv`；登入 20 天後使用會重發 cookie、第 45 天仍有效；閒置 31 天失效；連續使用 100 天仍有效；`/healthz` 與靜態檔不重發 cookie 且不查詢資料庫；被竄改的 cookie 失效；production 的 `__Host-`／`Secure` 屬性不變
- [X] 2.2 `CurrentUser` 新增 `restriction`；`require_login`／`login_required_page` 改為接受允許的受限狀態集合並依狀態拒絕或導向（D2）；`app/services/errors.py` 新增 `AccountLocked`（403、`account_locked`）；`AccessPolicy` 的待確認年齡判斷只適用角色 `user`。驗證：擴充 `tests/test_restricted_session.py`——對三種受限狀態各自掃描全部需要登入的端點，只有各自允許清單通過（含 `/api/auth/me`、`/api/auth/verify-age`、申訴 API、登出、`/account`、`/age`、`/appeal`）；同時被鎖定與待確認年齡時回 `account_locked`；管理員不進入待確認年齡狀態；`tests/test_access_policy.py` 通過
- [X] 2.3 `app/security/redirects.py` 的 `DEFAULT_AFTER_LOGIN` 改為 `/cs`，新增 `landing_for(user, next=None)`（D9）；`page_controller.home` 與 `/login`、`/register` 導向。驗證：新增 `tests/test_landing.py`——已登入一般玩家開 `/` 與 `/login` 導向 `/cs`；三種受限狀態各導向自己的頁面；`/register` 一律導向 `/login`；未登入開 `/` 仍是首頁；`next` 的安全檢查測試不變

## 3. Google 登入與年齡確認改版

- [X] 3.1 `GoogleSignInService.begin()` 只收條款旗標、`complete()` 忽略舊交易的年齡欄位；`_create_account` 不寫年齡並設定 `age_deadline_at`；發起 API 忽略請求中的生日與區間；Controller 依 `landing_for` 導向。驗證：改寫 `tests/test_google_sign_in_service.py`、`test_google_endpoints.py`——新使用者勾選條款 → 建立沒有生日的帳號、期限為建立時間加 24 小時、導向 `/age`；沒勾條款 → 不建立帳號、導回登入頁；請求帶生日被忽略且交易與資料庫都沒有；回訪者空的發起請求可登入且不被要求勾選；舊交易帶的 `b`、`r` 被忽略；既有的 state／nonce／PKCE／ID token 測試不變
- [X] 3.2 `AuthService.verify_age` 擴充（D5）：未滿 13 歲的玩家 → 以 `AccountErasureService` 抹除並清除 session、回 400 `age_not_eligible`；管理員只拒絕不抹除；不一致 → 通用 `age_mismatch` 並登記鎖定計次（`keep_changes_on_error`）；成功 → 寫入年齡資料並清空期限。驗證：改寫 `tests/test_age_gate_api.py`、`test_auth_registration.py` 中補填的部分——12 歲補填後帳號、Google 綁定、對話都不存在且資料庫與日誌找不到該生日；生日未滿但區間填成年仍以未滿 13 歲為準；管理員提交未滿 13 歲不被抹除；不一致回通用訊息、不留年齡資料、記一次計次；重複提交成功且不改變原紀錄；密碼註冊的不一致仍不留後果（`tests/test_auth_registration.py` 不變）
- [X] 3.3 新增 `/age` 頁面端點與 `GET /api/auth/age-rules`（若現有初始狀態已提供年齡規則則重用，不新增）；已完成者開啟導向預設頁面。驗證：新增 `tests/test_age_page.py`——待確認年齡者可開、已完成者導向、未登入導向登入頁並保留 `next`、補填成功後導向 `next`（不合法時預設頁面）

## 4. 逾時未補填的帳號清除

- [X] 4.1 新增 `app/services/unconfirmed_account_service.py`（或擴充 `AccountRetentionService`）與 `purge-unconfirmed-accounts` 指令（`--dry-run`、分批、條件式刪除當下再確認、走 `AccountErasureService` 並留下刪除紀錄、輸出只有筆數）；`SCHEDULED_COMMANDS` 新增 `"purge-unconfirmed-accounts": "0 * * * *"`（D6）。驗證：新增 `tests/test_cli_purge_unconfirmed_accounts.py`、`test_unconfirmed_account_service.py`——25 小時刪除、23 小時保留、剛好完成不刪、管理員與沒有期限的舊帳號不刪、預覽不刪除且不寫紀錄、重複執行不再刪除、輸出與日誌不含個資、並行完成年齡確認與清除不會刪到已完成的帳號；`tests/test_scheduled_commands.py` 與 `tests/test_launch_plan.py` 的排程清單納入新指令（每小時一次，其餘仍是每日）

## 5. 帳號鎖定與申訴

- [X] 5.1 新增 `AccountLockService`（D4）：`register_strike`、條件式鎖定（並行只鎖一次、已鎖定不再計次）、解除（清除鎖定欄位與計次）；Google 回呼拒絕（只有已辨識帳號者）、補填不一致、`UsageService._check_rate` 限流（拋出 `RateLimited` 前，`keep_changes_on_error`）三處呼叫；`app/services/registry.py` 組裝。驗證：新增 `tests/test_account_lock_service.py` 與擴充 `tests/test_chat_usage_limits.py`、`test_google_endpoints.py`——身分被拒（含補填不一致）15 分鐘內 5 次鎖定、第一次在 16 分鐘前則不計；限流 15 分鐘 10 次鎖定；無法辨識身分的失敗與沒有帳號的身分不計次、沒有鎖定；回應 4xx 後計次仍在資料庫；並行第 10 次限流只鎖定一次；鎖定後不會自動解除（48 小時後仍鎖定）；`account.lock` 稽核紀錄的管理員欄位為空且不含任何個資
- [X] 5.2 新增申訴服務與端點 `POST /api/appeals`、`GET /api/appeals/mine`、頁面 `/appeal`（D8）：1000 字、NFC 與控制字元清理、空白擋下、每小時 3 則（429）、未處理 1 則（409）、未被鎖定者 404；寫 `appeal.submit` 稽核（只含識別碼與字數）。驗證：新增 `tests/test_appeal_api.py`——各上限與錯誤碼、未登入 401、未被鎖定 404、日誌與稽核紀錄掃描找不到申訴文字、被鎖定者可查看自己申訴的處理狀態
- [X] 5.3 被鎖定者的登入流程：Google 回呼成功後建立受限登入狀態並導向 `/appeal`；`/api/auth/me` 回傳 `restriction`。驗證：新增 `tests/test_locked_login_flow.py`——被鎖定者通過 Google 後只能呼叫允許清單，客服對話與匯出回 403 `account_locked`；管理員解除後恢復

## 6. 管理員二階段登入

- [X] 6.1 Google 回呼對角色 `admin` 的帳號核發 10 分鐘的簽章中間狀態 cookie 並導向 `/login/password`，不建立登入 session；`POST /api/auth/admin-password` 驗證密碼（沿用 5 次／15 分鐘鎖定與通用失敗訊息）後才 `login_user` 並清除中間狀態；`POST /api/auth/login` 對管理員一律走密碼錯誤路徑且不計失敗（D7）。驗證：新增 `tests/test_admin_two_step_login.py`——Google 通過但未輸密碼時 `/api/admin/overview` 為 401；兩者都通過可進後台；中間狀態 11 分鐘後失效；中間狀態不能存取任何需要登入的端點；管理員用密碼端點得到通用失敗且不計失敗次數；第一次以同信箱 Google 登入綁定後同樣要再輸密碼；連續 5 次密碼錯誤鎖定 15 分鐘（正確密碼也被拒）
- [X] 6.2 `POST /api/auth/admin-password/forgot`：只接受持有中間狀態的請求（其餘 404），呼叫重設密碼寄信（不再受 `EMAIL_VERIFICATION_REQUIRED` 控制，只需要寄信設定；缺少時按鈕不顯示、端點 404）；重設成功後 `token_version` 加 1 並寄通知信，中間狀態與既有 session 失效。驗證：擴充 `tests/test_admin_two_step_login.py` 與 `tests/test_email_flow_api.py`——按下後假寄信器收到一封信、回應不洩漏其他資訊；匿名請求 404 且沒有寄信；重設後舊 session 失效；權杖 1 小時、單次使用、GET 只顯示確認頁的既有測試不變

## 7. 管理員後台

- [X] 7.1 `AdminService` 與 `/api/admin/*` 新增 `GET /locked`、`GET /appeals`（只回傳被鎖定帳號的申訴，寫 `appeal.read`）、`POST /users/<id>/unlock`（重新輸入密碼、清除鎖定與計次、待處理申訴標為已處理、寫 `account.unlock`、失敗寫 `reauth.failed`）；`AUDIT_ACTIONS` 與 `DANGEROUS_ACTIONS` 納入新動作（`account.lock`、`appeal.submit`、`appeal.read`、`account.unlock`）。驗證：新增 `tests/test_admin_lockout_api.py` 並擴充 `tests/test_admin_api.py`、`test_admin_audit_repository.py`、`tests/architecture/test_admin_boundaries.py`——非管理員一律 404（含格式錯誤的請求）；解除成功／密碼錯誤；解除後申訴不再出現在管理員檢視；管理員無法經由申訴取得該帳號的對話全文；稽核紀錄不含申訴文字；列舉以外的動作被拒
- [X] 7.2 後台介面（`console.html`、`admin.js`、`admin-format.js`）新增「被鎖定的帳號」與「申訴」兩個區塊與解除操作（重新輸入密碼的既有互動），內容一律純文字。驗證：擴充 `tests/test_admin_page.py` 與 Node 單元測試（格式化函式）；`tests/architecture/test_frontend_rules.py` 通過（無 `v-html`、無 Bootstrap JS）

## 8. 前端頁面

- [X] 8.1 `login.html`／`login.js` 改為只有 Google 按鈕與條款勾選框（按鈕在未勾選時仍可按、處理中禁用、`finally` 恢復、錯誤代碼以固定繁體中文顯示，含「請先同意條款」）；移除 `register.html`／`register.js`；`/register` 導向 `/login`。驗證：改寫 `tests/test_auth_pages.py`、`tests/js/` 相關測試——頁面沒有電子郵件、密碼、生日、年齡欄位；`/register` 導向；錯誤代碼對照；`tests/test_ui_components.py` 與 `tests/architecture/test_frontend_rules.py` 通過
- [X] 8.2 新增 `age.html`／`age.js`（生日與年齡區間、通用錯誤、`age_not_eligible` 後回 `/login`、`finally` 恢復）、`appeal.html`／`appeal.js`（字數提示、處理狀態）、`login_password.html`／`login_password.js`（密碼欄位與「忘記密碼」按鈕，寄信設定缺少時不顯示）；`account.js` 的年齡補填區塊只留給管理員。驗證：新增 `tests/test_age_page.py`、`test_appeal_page.py`、`test_admin_password_page.py` 的頁面部分；Node 測試涵蓋三個頁面的狀態機（送出中、成功、各種錯誤代碼）
- [ ] 8.3 瀏覽器人工驗收（使用者在場，沿用 `staging-deployment` 的 ngrok 通道與預備環境專用 OAuth 用戶端）：新使用者勾選條款 → Google → 年齡確認頁 → `/cs`；登出再登入一次點擊；未勾選條款的新使用者被擋；24 小時清除（以指令預覽）；被鎖定（以 `flask` 腳本製造）→ 申訴 → 管理員解除；管理員 Google 後輸入密碼。驗證：結果逐項記入 README；使用者不在場則標為未驗證

## 9. 告警、個資與日誌

- [X] 9.1 告警改名並改計數來源（D10）：`alert_rules`、`alert_messages`、`alert_service` 的顯示名稱為「帳號鎖定激增」，代碼 `login_lockouts` 不變；`count_locked` 合計兩種鎖定。驗證：改寫 `tests/test_alert_rules.py`、`test_alert_queries.py`、`test_alert_messages.py`、`test_alert_service.py`、`test_alerting_flow.py`——5 個鎖定為警告（含兩種合計）、同一帳號只算一個、密碼鎖定到期與管理員解除後不計；信件主旨與內文用新名稱
- [X] 9.2 個人資料匯出納入申訴（JSON 與 CSV，不含計次與他人資料）；`purge-expired-data` 納入 7 天前的鎖定計次（預覽只列筆數）；結構化日誌事件（鎖定、解除、申訴、年齡抹除）只含事件名稱、內部編號與固定列舉。驗證：擴充 `tests/test_personal_export_service.py`、`test_account_export_api.py`、`test_data_retention_service.py`、`test_cli_purge_expired_data.py`、`test_log_scan_full_flow.py`——申訴出現在匯出、被鎖定者匯出回 403、8 天前的計次被清除而 6 天前的保留、申訴不被期限清除、日誌掃描找不到申訴文字、生日或 email

## 10. 測試輔助、法律頁面與既有測試改寫

- [X] 10.1 `tests/support/client.py` 的 `csrf_token()` 改取 `/terms`（已登入的 client 打 `/` 會被導向而拿不到 CSRF cookie，D9）；新增測試輔助「直接寫入登入 session」與「假的 Google 供應商登入」以取代管理員的密碼登入；改寫 `tests/test_admin_flow.py`、`test_alerting_flow.py`、`test_privacy_lifecycle_flow.py`、`test_content_ceiling_switch.py`、`test_account_api.py`、`test_auth_api.py`、`test_email_flow_api.py`、`test_security_headers.py` 中依賴管理員密碼登入或註冊時填年齡的部分。驗證：這些檔案全部通過，且沒有任何測試被刪除而未被等價的新測試取代（以任務 2 至 9 的新測試涵蓋）
- [X] 10.2 隱私權政策與服務條款頁面（D11）補上：先蒐集 Google 提供的信箱與名稱、年齡確認 24 小時內未完成或未滿 13 歲時立即刪除、帳號鎖定與申訴的資料處理；`CURRENT_TERMS_VERSION` 提升。驗證：`tests/test_legal_pages.py`、`tests/test_legal.py` 通過並新增上述文字的斷言

## 11. 文件與整體驗證

- [X] 11.1 更新 docs/ 對應的設計文件（對照表見 docs/README.md）：帳號與安全（登入流程圖、受限狀態、年齡確認時序、鎖定與申訴、管理員二階段、session 續期）、資料模型（新欄位與兩張表、migration 歷程）、系統架構（`AccountLockService`、`SlidingSessionInterface`、請求生命週期）、前端結構（新頁面與移除的註冊頁）、營運與部署（新排程指令、告警改名、上線前關卡 1 仍待 change ②）、路線圖；拿掉各文件與 README、15 個主規格上「規範已於 2026-11-02 修訂，實作待 change ① 與 ②」中屬於 ① 已完成的部分；README 的端點表、環境變數說明與驗收紀錄同步。驗證：`pytest tests/test_design_docs.py tests/test_readme.py tests/test_launch_docs.py tests/test_agents_skill_sync.py` 通過
- [x] 11.2 全量驗證：`pytest` 全量（含 SQL Server，不略過）、`node --test`、容器驗收 `scripts/verify_container.py`（改寫其管理員流程）、`openspec validate google-first-login --strict`。驗證：四者通過，數字填入 README 驗收紀錄與路線圖

## Workflow follow-up

- 檢視後提交，再以 `/openspec-archive-change google-first-login` 同步主規格並封存。
- 接著 propose change ②（移除玩家端密碼註冊／登入／忘記密碼／信箱驗證與玩家端寄信，上線前關卡 1 改為「營運告警的寄信設定」）。
- `cloud-staging-run` 的 Google 登入人工驗收要在本 change 之後做。
