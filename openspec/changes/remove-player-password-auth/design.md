# Design

## Context

change ①（`google-first-login`，已封存）讓 Google 成為玩家唯一的實際入口，並把管理員改為「先 Google、再密碼」。玩家端的密碼註冊、密碼登入、忘記密碼與信箱驗證仍在程式裡：`AuthService.register/login`、`EmailFlowService` 的驗證與玩家忘記密碼、`/api/auth/*` 的五個端點、兩個頁面與腳本、`EMAIL_VERIFICATION_REQUIRED`（在 `AppConfig`、`AccessPolicy`、`AuthService`、`AdminService`、`launch_plan.py`、`loadtest/fake_app.py` 都有分支）。動機見 proposal 的 Why。

盤點時發現三個 proposal 之外容易漏掉的相依，它們決定了本設計的形狀：

1. **客服提示詞的「平台資料」**（`cs_prompt.py`）仍教模型說「註冊需要信箱與密碼」「忘記密碼」「信箱驗證信」；移除功能而不改它，客服會對使用者說出不存在的流程。
2. **負載測試、冒煙驗收與容器驗收都靠 `/api/auth/register` + `/login` 取得玩家身分**（`loadtest/actions.py`、`scripts/staging_smoke.py`、`scripts/verify_container.py`）。端點移除後這些工具沒有任何登入途徑，而 Google 登入無法自動化。
3. **申請刪除帳號的通知信**走 `EmailFlowService`，由「信箱驗證開啟」這個旗標決定寄不寄；旗標消失後必須明確決定它的去留。

既有約束：migration 為部署前獨立步驟、expand/contract、不得含方言專屬原生 SQL、不依賴應用程式程式碼（見 AGENTS.md 第 4 節與 0010 的寫法）；玩家不設密碼（第 10 節）。

## Goals / Non-Goals

**Goals:**
- 玩家端的密碼、信箱驗證、玩家忘記密碼與寄信相關程式、設定、頁面、腳本與規格全部移除，且沒有行為殘留（`pending_verification`、`email_verification_required` 參數、死分支）。
- 管理員的所有流程（二階段登入、忘記密碼、變更密碼、重新輸入密碼）行為不變。
- 既有玩家資料平順過渡：舊密碼雜湊被清除；以同信箱的 Google 登入回到原帳號。
- 自動化驗收（載入測試、冒煙、容器）在沒有密碼登入的情況下仍能以測試玩家身分運作，且映像不新增任何能簽發登入狀態的途徑。

**Non-Goals:**
- 不強制 staging／production 必須設定 Google（會牽動容器驗收與本機預演；沒有 Google 時玩家無法登入，已由上線前關卡涵蓋）。
- 不刪除任何欄位或資料表；`password_hash`、`email_verified_at`、`failed_login_count`、`locked_until`、`auth_tokens` 都還有管理員或 Google 在用。
- 不清理「已設定密碼者刪除帳號要輸入密碼」這條已不可達的分支（管理員不能自助刪除，玩家沒有密碼）；它無害，另案處理。
- 不改變 AGENTS.md（已是目標狀態）。

## Decisions

### D1：移除端點與頁面，讓它們回 404，不留 410 或導向
五個 API 與兩個頁面直接從路由表移除（Flask 預設的 404 與 JSON 錯誤格式即可），`/register` 維持導向 `/login`。*理由*：留一個「已淘汰」的回應等於維護一條沒人走的路徑；404 與「從未存在」一致，也不洩漏曾經的帳號機制。*替代*：回 410／導向 — 多一層測試與文案，沒有使用者受益（沒有外部客戶端呼叫這些 API）。

### D2：資料遷移 `0011` 只動資料，用輕量資料表表達式，不匯入應用程式程式碼
`migrations/versions/…_0011_remove_player_passwords.py`：
- `users`：`role <> 'admin'` 的列，`password_hash=NULL`、`failed_login_count=0`、`locked_until=NULL`；其中**曾持有密碼雜湊的**另外 `token_version` 加 1（先加再清，所以重複執行不會再加）——它們的既有登入狀態隨憑證一起失效，包含遷移前被預先佔用信箱的攻擊者靠密碼取得的 cookie；純 Google 玩家與管理員的登入狀態不受影響。
- `users`：`status='pending_verification'` → `'active'`；`status_before_deletion='pending_verification'` → `'active'`。
- 以 `sa.table()`／`sa.column()` 加 `op.get_bind().execute(sa.update(...))` 表達，不是原生 SQL，也不匯入 `app.models`（與 0010 一致，避免未來模型變動讓舊 migration 壞掉）。可重複執行（冪等）。
- `downgrade()` 不做任何事並在註解說明：被清除的雜湊無法還原，回復靠遷移前的備份。

*理由*：把已無用途的密碼雜湊留在資料庫是持續的個資與攻擊面；而改寫狀態讓程式碼可以乾淨地移除 `pending_verification`。*替代*：(a) 不遷移、程式容忍舊值 — 程式要永久帶著 `pending_verification` 的分支；(b) 刪除未驗證的舊帳號 — 刪除走抹除出口且需要 tombstone，對一個目前只有測試帳號的專案太重；改為 active 後它們只能由 Google 登入取得，合併規則（未驗證者清除憑證並視為已驗證）本來就處理了預先佔用信箱的情況。
*相容性*：遷移先於新版程式執行；此時跑的是舊版程式，它對「玩家沒有密碼、狀態為 active」的帳號已有既有路徑（`no_password`、純 Google 帳號），所以順序安全。`email_verified_at` 為空的舊列保持為空，讓 Google 合併規則仍能辨識「未驗證的遺留帳號」。

### D3：`pending_verification` 與 `TOKEN_VERIFY_EMAIL` 自程式移除
`USER_STATUSES` 只剩 `active`、`suspended`、`deletion_pending`；`User.status` 沒有 CHECK 約束，所以不需要結構變更。`AdminRepository.unsuspend` 與 `AdminService` 的還原邏輯一律回 `active`；`UserRepository` 的刪除還原規則簡化為「還原為申請前的狀態（只會是 active）」。權杖用途只剩 `reset_password`；`auth_tokens` 的清除依到期時間，不看用途，所以遺留的驗證權杖會自然被清掉（7 天內）。

### D4：`EmailFlowService` 縮成管理員的重設流程
保留：`request_admin_password_reset`、`reset_password`（新增「權杖所屬帳號必須是管理員」的檢查，防禦遺留的玩家重設權杖）、`send_password_changed_notice`、權杖發放／消耗／頻率限制。移除：`send_verification`、`resend_verification`、`verify_email`、`request_password_reset(email)`、`send_deletion_notice`、`_mark_verified`。`AuthService.change_password` 的通知信改為「已有寄信器時才寄」，條件從旗標改為 `mailer is not None`；`AccountMailer` 協定只剩 `send_password_changed_notice`。

### D5：玩家不再收到任何信（含刪除通知）
`AccountDeletionService` 移除 `mailer` 與 `DeletionMailer`，`mail_messages.deletion_notice_mail`、`verification_mail`、`verification_link` 一併刪除。*理由*：使用者的動機就是不為玩家維運寄信服務；申請成功的畫面與 `/account` 本來就顯示預定刪除時間與撤銷方式。*替代*：有寄信器就寄 — 玩家是否收到信會因環境而異，且仍需為玩家信件的送達率、退信與隱私負責，違背動機。這是行為變更（proposal 已標註），不是單純刪除死碼。

### D6：寄信設定改由營運用途決定
`app/config.py`：移除 `_resolve_email_verification`、`AppConfig.email_verification_required`。寄信設定在 `ALERT_EMAIL_TO` 非空**或**任何一項 `MAIL_SMTP_HOST`／`MAIL_SMTP_PORT`／`MAIL_FROM` 非空時解析並要求完整；否則 `mail=None`。`BASE_URL` 只在 Google 啟用時必填。`create_app` 仍以 `cfg.mail` 建立寄信器。管理員重設的可用性仍是 `admin_password_reset_available()`（寄信器加 `BASE_URL`）。
*為什麼「任何一項」而不是只看告警*：development 想測管理員重設信時只設 `MAIL_*` 就能做，且半套設定會明確失敗而不是被默默忽略。
*舊變數*：讀到 `EMAIL_VERIFICATION_REQUIRED` 時只記一則警告（變數名稱，不含值），不影響啟動——正式環境的平台設定留著它不該讓部署失敗；`launch_plan.py` 的部署計畫檔則把它視為不認得的變數而拒絕，因為那是我們自己產生部署內容的地方，應該被迫清乾淨。

### D7：`AccessPolicy` 不再問信箱是否驗證
`_account_usable` 只看 `status == active`。玩家的信箱在建立帳號時就由 Google 驗證（`email_verified_at` 有值），管理員由 `create-admin` 建立時也設為已驗證；遷移後唯一 `email_verified_at` 為空且 `active` 的是遺留的舊帳號，它們無法登入（沒有憑證），直到以 Google 登入而被標為已驗證。所以這個檢查在移除後沒有任何可到達的作用。`AdminService.get_user` 之類回傳的 `email_verified` 欄位保留（它描述的是事實，不是開關）。

### D8：自動化驗收改用「在目標環境內建立測試玩家並簽出 session cookie」
共同做法：一個主機端 Python 輔助模組（`scripts/mint_test_sessions.py`）產生一小段程式，**在目標環境的程序內執行**（`docker exec … python -c`、或本機以同樣的環境變數載入），它：拒絕 `APP_ENV=production`；以應用程式工廠建立 N 位 `@loadtest.invalid` 的玩家（`active`、已完成年齡確認的成年人、條款已同意、無密碼，走 `UserRepository.add_user`）；用 `app.session_interface` 的簽章器以該環境自己的 `SECRET_KEY` 簽出與真實登入完全相同的 session cookie（內容只有 `uid`、`tv`）；以 JSON 印出。這與 `verify_container.py` 對管理員已採用的做法相同，只是抽成可重用的單一實作。
- `loadtest`：`Member` 改為從 cookie 檔取得登入狀態（`register_and_login` → `sign_in`）；cookie 檔不足就在送出任何請求前失敗；`fake_app` 不再設定驗證旗標。
- `staging_smoke.py`：需要登入的項目只在環境變數提供 cookie 時執行，否則標「未驗證」（不是通過）；新增「已移除端點回 404」的檢查。
- `verify_container.py`：玩家流程改為簽出 cookie 後檢查（`/api/auth/me`、`/account`、CSRF、登出），並檢查五個端點回 404。
- **映像不新增任何能簽發登入狀態的指令或端點**；輔助模組在 `scripts/`（不進映像）。
*替代*：(a) 在 `fake_app` 與 staging 加「測試登入」端點 — 在 staging 設定中放一個免認證登入，風險與收益不成比例；(b) 新增 `flask mint-session` 指令進映像 — 一個在正式映像裡能簽 cookie 的指令，即使有環境檢查也是不必要的攻擊面；(c) 壓測直接放棄需登入的情境 — 失去容量證據的核心。

### D9：客服提示詞的平台資料改寫，且數字取自常數
`_platform_facts()` 改為描述 Google 登入、條款勾選、登入後填生日與區間（期限取自 `AGE_CONFIRMATION_DEADLINE`、最低年齡取自年齡政策）、被鎖定時只能申訴、帳號頁的登出所有裝置／匯出／刪除；`_INSTRUCTION_TEMPLATE` 服務範圍第 1 項與 `cs_intents.INTENT_ACCOUNT_HELP` 的說明移除密碼與信箱驗證。不再匯入 `MIN_PASSWORD_LENGTH`、`MAX_FAILED_LOGINS`、`VERIFY_TTL`、`RESET_TTL`。測試比對提示詞不含已移除功能的字樣，且與意圖列舉、結構描述三處一致的既有測試照常通過。

### D10：順序與切分
後端（遷移 → 設定 → 服務 → 控制器）→ 前端 → 客服提示詞 → 驗收工具 → 測試清理 → 文件。每一段結束時整套測試應為綠色：先讓新程式不依賴被移除的東西，再刪除；測試的大規模刪改放在對應程式刪除的同一段，避免出現長時間紅燈。

## Risks / Trade-offs

- **[遷移不可逆]** 玩家密碼雜湊被清除後無法還原 → 遷移前備份（既有部署步驟）；舊版程式在遷移後仍可運作，所以回復映像不需回復資料。目前僅有測試帳號，實際損失為零。
- **[遺留玩家以不同信箱的 Google 登入會變成新帳號]** 密碼註冊用的信箱與其 Google 帳號信箱不同時，無法回到原帳號 → 不處理（舊帳號 12 個月閒置後由既有排程刪除）；README 與營運文件註明，且目前無真實玩家。
- **[測試 cookie 是真實有效的登入憑證]** 簽出的是 `@loadtest.invalid` 測試玩家，無任何真實資料；輔助模組拒絕 production；cookie 檔被 `.gitignore` 排除、輸出不進日誌；預備環境本來就不含真實個資 → 風險限於預備環境的測試帳號。
- **[客服提示詞漏改]** 模型會說出不存在的流程 → 以「提示詞不含已移除功能字樣」的測試把關，並在 tasks 列出對照清單。
- **[大範圍測試改動]** 約 20 個測試檔涉及 → 先以 `tests/support/auth.py` 的 `sign_in_as` 取代密碼登入夾具，再刪除只測被移除行為的測試；每段結束跑完整測試，並比對「刪除的測試數」與「被移除的需求」是否一一對應，避免順手刪掉仍有效的覆蓋。
- **[部署計畫檔拒絕舊變數]** 既有的 `*.vars` 若含 `EMAIL_VERIFICATION_REQUIRED` 會被 `launch_plan.py` 拒絕 → 錯誤訊息明確指出變數名稱與「已淘汰，請移除」；這是刻意的一次性清理。

## Migration Plan

1. 備份資料庫（既有步驟）。
2. 執行 `alembic upgrade head`（`0011`，只動資料，可重複執行）。
3. 部署新版程式；平台設定可保留或移除 `EMAIL_VERIFICATION_REQUIRED`（啟動會警告一次）。
4. 驗證：`/healthz`、`/readyz`；`POST /api/auth/login` 回 404；以 Google 登入一次；管理員走一次「Google → 密碼」。
5. 回復：映像回到前一版即可（無結構變更；舊版程式能處理遷移後的資料）；若需要還原玩家密碼才需要用備份，但舊版程式不再有玩家密碼入口，所以通常不需要。

## Open Questions

- 是否要在後續把「staging／production 必須設定 Google」做成啟動檢查？（另案；取決於容器驗收與本機預演如何在沒有 Google 的情況下繼續啟動。）
- 已不可達的「已設定密碼者刪除帳號要輸入密碼」分支何時清除？（另案，不影響行為。）
