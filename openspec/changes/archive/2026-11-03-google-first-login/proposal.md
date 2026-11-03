# Proposal

## Why

`staging-deployment` 的 Google 登入人工驗收發現：登入頁每次都要求重填條款、生日與年齡區間，沒有「回訪一鍵登入」，`/` 也不會導向已登入者的頁面。使用者因此重新定義了玩家的登入體驗，並已把規範先修訂完成（AGENTS.md 第 3、8、9、10、11 節，report.md 的 U-12 至 U-19）：玩家只用 Google 登入，畫面只有 Google 登入與條款勾選框，生日與年齡區間改在登入**之後**才填，沒填什麼都不能做，帳號最多保留 24 小時；登入有效期改為閒置 30 天、每次使用就續期；管理員每次先 Google 登入再驗證一次密碼；加上防洗版的帳號鎖定與申訴。本 change 把這些規範落實到程式。

## What Changes

- **登入與註冊合一為 `/login`**：只有「使用 Google 登入」與一個條款勾選框（新使用者必勾、回訪不必勾）。移除 `/register` 頁面（導向 `/login`）。已登入者打開 `/` 或 `/login` 直接導向預設頁面（單一常數，現為 `/cs`，不再是 `/account`）。
- **登入後才填年齡**：首次 Google 登入建立帳號時不再要求生日與年齡區間（發起時不再驗證年齡）；帳號建立時記下 24 小時期限。完成前除了「完成年齡確認」與「登出」，所有頁面與 API 一律拒絕（API 回 403 `age_confirmation_required`，頁面導向新的年齡確認頁 `/age`）。補填時未滿 13 歲：立即抹除帳號與個資、不保存生日；兩者不一致：回通用的「資料有誤」並計入帳號鎖定的次數。
- **逾時未補填就刪除**：新增每小時執行的排程指令，刪除建立已超過 24 小時仍未完成年齡確認的玩家帳號（管理員不納入）。
- **登入有效期改為閒置 30 天、每次使用就續期**：取代 7 天絕對期限；cookie 內容不變（仍只有使用者編號與 `token_version`），續期靠重新簽章。
- **帳號鎖定與申訴**：同一帳號 15 分鐘內 Google 登入被拒達 5 次（含補填年齡的不一致），或被每分鐘訊息數上限擋下達 10 次，自動鎖定，只有管理員能解除。被鎖定者通過 Google 後只得到受限登入狀態，只能進申訴頁向管理員留言；其餘一律 403 `account_locked`。管理員在後台看到被鎖定的帳號與其申訴並可解除，鎖定、申訴、解除都寫入稽核紀錄。「登入失敗激增」告警改稱「帳號鎖定激增」，並把這類鎖定一併計入。
- **管理員二階段登入**：管理員通過 Google 後還要再驗證一次密碼才建立登入 session（中間狀態只存在短效簽章 cookie）；管理員不能只靠密碼登入。密碼驗證那一步有「忘記密碼」按鈕，寄重設信到該管理員的信箱，沿用既有的重設流程。
- **受限登入狀態的一般化**：「等待刪除」之外新增「尚未完成年齡確認」與「被鎖定」兩種受限狀態，一律預設拒絕，由同一個自動化掃描測試證明。
- **資料與維運**：`users` 新增年齡期限與鎖定欄位，新增兩張資料表（鎖定計次、申訴），一個 migration；新增排程指令；申訴納入個人資料匯出；計次紀錄納入期限清除。
- **不在本 change**（見 `google-first-login` 之後的 change ②）：移除玩家端的密碼註冊／登入／忘記密碼／信箱驗證／玩家端寄信與 `EMAIL_VERIFICATION_REQUIRED`、production 的寄信要求、上線前關卡 1 的改寫。本 change 結束時 `/api/auth/register` 與 `/api/auth/login` 仍存在（管理員以外的密碼登入由 change ② 移除；登入頁已看不到它們）。另不含：入口層 IP 限流（使用者先不做）、RPG 的選擇劇本首頁、對匿名濫用的防護。
- **BREAKING**：登入有效期語意改變（7 天絕對 → 閒置 30 天續期）；`/register` 頁面移除；管理員無法再只靠密碼登入；尚未完成年齡確認的玩家對所有功能（不只 AI 功能）被拒；既有測試中假設「註冊時填生日」或「登入後進 `/account`」的部分要改寫。

## Capabilities

### New Capabilities
- `post-login-age-confirmation`: 首次 Google 登入建立「待確認年齡」的帳號；完成前只能做年齡確認與登出、一律導向年齡確認頁；24 小時期限與逾時清除；補填時未滿 13 歲立即抹除、不一致回通用訊息。
- `account-lockout`: 以帳號為單位的自動鎖定（Google 登入被拒、被訊息數上限擋下的累計）、只有管理員能解除、受限登入狀態、管理員檢視被鎖定帳號與解除、稽核。
- `lockout-appeals`: 被鎖定者的申訴頁與 API（長度、頻率、待處理上限）、管理員只看得到被鎖定帳號送出的申訴、申訴視為個資。
- `admin-two-step-login`: 管理員 Google 之後的密碼驗證、短效中間狀態、不能只靠密碼登入、密碼驗證步驟的「忘記密碼」寄信重設。

### Modified Capabilities
- `session-authentication`: 登入 cookie 改為閒置 30 天、每次使用就續期、不設絕對上限。
- `google-sign-in`: 首次登入不再要求年齡（改為建立待確認年齡的帳號）；回訪者一鍵登入。
- `age-confirmation`: 「未滿 13 歲不得註冊」改為補填時立即抹除；補填年齡的規則（補填 API 的行為、對照不一致計入鎖定）更新。
- `auth-pages`: 登入頁只有 Google 與條款；移除註冊頁與註冊完成畫面；新增年齡確認頁；登入後的預設頁面與首頁導向。
- `access-control`: 受限登入狀態預設拒絕一般化為三種受限狀態與各自的允許清單。
- `operational-alerts`: 「登入失敗激增」改稱「帳號鎖定激增」，計入新的鎖定。
- `admin-audit-log`: 稽核動作列舉新增鎖定相關動作。
- `account-retention`: 新增逾時未完成年齡確認的帳號清除。
- `data-retention`: 新增鎖定計次紀錄的期限清除。
- `personal-data-export`: 匯出內容納入申訴。

## Impact

- **程式**：`app/security/session.py`（續期的 session 介面、受限狀態一般化）、`app/security/redirects.py`、`app/services/google_sign_in_service.py`、`auth_service.py`、`access_policy.py`、`current_user.py`、`usage_service.py`（洗版計次）、`account_retention_service.py` 或新服務（逾時清除）、新的鎖定與申訴服務與 Repository、`admin_service.py` 與 `/api/admin/*`（鎖定名單、申訴、解除）、`alert_rules.py`／`alert_service.py`（改稱與計入）、`app/cli.py`（`SCHEDULED_COMMANDS` 新增每小時指令）。
- **前端**：`login.html`／`login.js`（Google 與條款）、新增 `age.html`／`age.js` 與 `appeal.html`／`appeal.js`、管理員密碼驗證步驟頁、後台的鎖定與申訴區塊；移除 `register.html`／`register.js`（`/register` 導向 `/login`）。
- **資料庫**：一個 Alembic migration（expand 階段，只新增可為空的欄位與新表）：`users.age_deadline_at`、`users.locked_at`、`users.lock_reason`、`lock_strikes`、`lock_appeals`。SQLite 與 SQL Server 雙軌。
- **規格與文件**：上列主規格；`docs/` 的帳號與安全、資料模型、系統架構、營運與部署、前端結構；README；隱私權政策與服務條款頁面（揭露先蒐集 Google 信箱與名稱、24 小時刪除、申訴與鎖定）。
- **部署**：`launch_plan` 的排程由 `SCHEDULED_COMMANDS` 產生，新增的每小時指令自動進入計畫；`cloud-staging-run` 的 Google 登入人工驗收要在本 change 之後做才有意義。
- **風險**：①受限狀態一般化若漏了某個端點，會讓待確認年齡或被鎖定者能使用功能——用既有的「掃描所有路由」測試擴充到三種狀態來守住；②既有（舊流程建立、沒有生日）的玩家帳號會被新閘門擋住但不會被 24 小時清除（沒有期限欄位的不清除，見 design）；③洗版與登入被拒的計次若與錯誤回應同一交易回復就會漏記，要沿用「錯誤時仍保留變更」的既有機制；④登入有效期不設絕對上限是使用者明確接受的風險（撤銷靠 `token_version`）；⑤`/api/auth/register`、`/api/auth/login` 在本 change 結束時仍可呼叫（change ② 才移除），所以「玩家只用 Google」在 API 層要等 ② 才完全成立。
