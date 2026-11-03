# Proposal

## Why

AGENTS.md 第 10、11 節已改為「玩家只用 Google 登入」：玩家不設密碼，沒有信箱驗證信、玩家端的重設密碼信與 `EMAIL_VERIFICATION_REQUIRED`，正式環境也不再因玩家端而要求寄信服務。change ①（`google-first-login`）已讓 Google 成為玩家唯一的實際入口，但舊的密碼註冊、密碼登入、忘記密碼、信箱驗證與其設定開關仍留在程式與規格裡，成了沒有人使用、卻仍要維護與防護的攻擊面（可預先佔用信箱、可猜密碼、可觸發寄信）。現在移除，才能讓程式、規格與規範一致，並把上線前關卡 1 從「信箱驗證與寄信」改成「營運告警的寄信設定」。

## What Changes

- **BREAKING（API）移除玩家端的密碼與信箱驗證端點**：`POST /api/auth/register`、`/login`、`/verify-email`、`/resend-verification`、`/forgot-password`，以及頁面 `/forgot-password`、`/verify-email`（改為不存在，回 404）。`/register` 仍導向 `/login`。`/api/auth/reset-password` 與 `/reset-password` 頁面保留，但只服務管理員。
- **BREAKING（設定）移除 `EMAIL_VERIFICATION_REQUIRED`**：`.env.example`、`AppConfig.email_verification_required`、`scripts/launch_plan.py` 的變數清單與 production 必須開啟的檢查一併移除。應用程式讀到這個變數時忽略它並在啟動時警告一次（不回顯值），避免殘留設定讓部署失敗；部署計畫檔仍含它則視為不認得的變數而被拒絕，逼使用者清掉。
- **寄信設定改為只由營運用途決定**：寄信（SMTP）設定在設定了 `ALERT_EMAIL_TO` 或任何一項 `MAIL_*` 時才解析，且一旦解析就要求完整（缺漏即啟動失敗）；staging 與 production 本來就必填 `ALERT_EMAIL_TO`，所以實際行為不變，但不再有「為玩家驗證信而必須寄信」的路徑。`BASE_URL` 只在 Google 登入啟用時必填（OAuth 回呼）；管理員忘記密碼的連結仍需要寄信設定與 `BASE_URL`，缺少時按鈕與頁面不出現（現況不變）。
- **玩家不再收到任何信**：申請刪除帳號的通知信一併移除（畫面本來就顯示預定刪除時間與撤銷方式）。管理員的重設密碼信、密碼已變更通知信與營運告警信保留。
- **帳號狀態 `pending_verification` 退役**：不再有任何程式產生它。以資料遷移 `0011` 把既有的 `pending_verification`（含 `status_before_deletion`）改為 `active`，並清除所有非管理員帳號的 `password_hash`、失敗計數與密碼鎖定（玩家不設密碼，舊的密碼雜湊是不再有用途的個資）。遷移只動資料、不動結構（expand 階段）；既有玩家以同信箱的 Google 登入即綁定回原帳號（已驗證者直接綁定；未驗證者依既有合併規則清除憑證後綁定）。`users.password_hash`、`email_verified_at`、`failed_login_count`、`locked_until`、`auth_tokens` 都留給管理員使用，沒有欄位或資料表被刪除。
- **權限與服務簡化**：`AccessPolicy` 不再依「信箱驗證開關」判斷帳號是否可用（帳號 `active` 即可，玩家的信箱在建立時就已由 Google 驗證）；`AuthService` 移除註冊與密碼登入；`EmailFlowService` 移除驗證、重寄與玩家忘記密碼，重設密碼只接受管理員的權杖；後台停權／解除停權不再有 `pending_verification` 的目標或還原狀態。
- **管理員不受影響**：`create-admin`、Google 加密碼的二階段登入、`/api/auth/admin-password`、管理員忘記密碼、`/api/auth/change-password`、危險操作的重新輸入密碼都維持原樣。
- **載入測試與容器驗收不能再用密碼註冊／登入**：改為「在目標環境內建立已完成年齡確認的測試玩家，並以該環境自己的 `SECRET_KEY` 簽出與真實登入相同的 session cookie」（與容器驗收對管理員已採用的做法相同），映像內不新增任何能簽發登入狀態的指令或端點。`loadtest`、`scripts/verify_container.py`、`scripts/staging_smoke.py` 與假模型入口隨之調整。
- **客服提示詞**：`app/services/cs_prompt.py` 的「平台資料」目前還在教模型說「註冊需要信箱與密碼」「忘記密碼」「信箱驗證信」，這些事實一旦移除就是錯的；改為如實描述 Google 登入（含年齡確認期限與被鎖定時的申訴），`account_help` 意圖說明同步改寫。
- **前端**：刪除 `forgot_password.html`、`verify_email.html` 與對應 JS；`/account` 移除「尚未驗證」提示與重寄按鈕；`reset-password` 頁文字改為管理員用語。
- **文件與規範副本**：更新 README 驗收紀錄的相關描述、`docs/` 設計文件（帳號與安全、系統架構、資料模型、營運與部署、前端結構、路線圖）、`.claude/skills/python-flask-skill` 的 auth-security 參考；AGENTS.md 本身已是目標狀態，不改。
- **不做**：不強制 staging／production 必須設定 Google 登入（會牽動容器驗收與本機預演，另案決定；沒有 Google 時玩家無法登入，已由上線前關卡涵蓋）；不刪除任何欄位或資料表（contract 階段留待下一個版本，且目前沒有需要刪除的欄位）；不處理 change ① 已知限制中與密碼無關的項目。

## Capabilities

### New Capabilities

（無。所有行為都落在既有能力上。）

### Modified Capabilities

- `member-registration`: 移除密碼註冊的輸入驗證、不洩漏帳號是否存在、依信箱驗證設定而定的結果、註冊時記錄條款同意；保留會員資料模型、email 正規化與唯一性、條款與隱私權頁面（資料模型的狀態值移除 `pending_verification`）。
- `email-token-flows`: 移除信箱驗證與重寄驗證信、信箱驗證關閉時停用相關功能；忘記密碼與重設密碼只剩管理員；信件內容只剩重設密碼與密碼已變更通知；權杖用途只剩重設密碼。
- `session-authentication`: 移除密碼登入的玩家情境；密碼雜湊、失敗訊息、失敗鎖定、變更密碼改以管理員為主體；帳號狀態與登入規則移除 `pending_verification`；移除 `/api/auth/login`。
- `auth-pages`: 頁面清單移除 `/forgot-password` 與 `/verify-email`；確認頁需求只剩 `/reset-password`；`/account` 移除信箱驗證提示與重寄。
- `google-sign-in`: 同信箱合併的情境不再以「密碼註冊後的未驗證帳號」為主，改述為遺留資料；移除「信箱已有純 Google 帳號時以信箱註冊」。
- `age-confirmation`: 移除密碼註冊時的年齡確認、未滿 13 歲拒絕註冊、不一致後重新註冊等情境，保留登入後補填與管理員補填。
- `access-control`: 帳號可用性不再依信箱驗證開關；移除「待驗證帳號使用客服」與「網頁註冊不能取得管理員」的註冊情境。
- `admin-user-management`: 停權與解除停權移除 `pending_verification`。
- `account-deletion`: 移除申請刪除的通知信、移除 `pending_verification` 還原情境。
- `app-configuration`: 移除「信箱驗證開關」；寄信與對外網址設定改以告警收件人與 `MAIL_*` 觸發；`.env.example` 不再列出該變數。
- `launch-gates`: 上線前關卡 1 改為「營運告警的寄信設定」。
- `service-observability`: 事件清單移除註冊、驗證、玩家登入失敗、忘記密碼等事件；完整流程的日誌掃描改為 Google 登入與管理員流程。
- `data-retention`: 權杖的清除描述改為重設密碼權杖。
- `admin-two-step-login`: 「管理員不能只靠密碼登入」改為密碼登入端點不存在（404）。
- `cs-nlp-contract`: `account_help` 意圖說明移除密碼與信箱驗證；新增「客服的平台資料如實描述 Google 登入」。
- `load-testing`: 虛擬使用者改為使用預先簽出的 session cookie；簽出步驟拒絕在 production 執行。
- `staging-verification`: 冒煙驗收不再註冊與登入，需要登入的項目只在提供測試 cookie 時執行（否則標為未驗證）；順帶把 Google 人工驗收的描述更新為 change ① 之後的流程。

## Impact

- **程式**：`app/controllers/auth_api_controller.py`、`auth_page_controller.py`；`app/services/auth_service.py`、`email_flow_service.py`、`access_policy.py`、`admin_service.py`、`account_deletion_service.py`、`current_user.py`、`mail_messages.py`、`registry.py`；`app/repositories/admin_repository.py`、`user_repository.py`；`app/models/user.py`（狀態值與權杖用途）；`app/config.py`、`app/__init__.py`（寄信器建立條件）；`app/observability/security_events.py`（事件常數）；`app/templates/auth/*`、`app/static/js/auth/*`（含 JS 單元測試與資源清單）；新增 migration `migrations/versions/…_0011_remove_player_passwords.py`（只動資料）。
- **工具**：`scripts/launch_plan.py`、`scripts/staging_smoke.py`、`scripts/verify_container.py`、`scripts/dev_smtp_sink.py`（用途改為管理員重設信）、`loadtest/*`、`.env.example`。
- **測試**：移除或改寫約 20 個測試檔中與密碼註冊、登入、信箱驗證、玩家忘記密碼有關的部分（多數情境已由 `tests/support/auth.py` 的 `sign_in_as` 取代）；新增 migration 與設定行為的測試。
- **文件**：README、`docs/` 六份設計文件、路線圖、技能參考檔；主規格 17 份有 delta（封存時一併改寫 `member-registration`、`email-token-flows`、`session-authentication` 的 Purpose），並移除 15 份規格頁首「待 change ② 更新」的提示。
- **部署**：沿用「備份 → migration → 部署」；`0011` 為不可逆的資料遷移（回復靠備份），舊版程式在遷移後仍可運作（玩家沒有密碼會走「沒有密碼」的既有路徑）。既有平台設定若仍含 `EMAIL_VERIFICATION_REQUIRED`，新版程式會忽略並警告，之後再自平台設定移除。
