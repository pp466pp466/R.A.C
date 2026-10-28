# Design

## Context

動機與範圍見 proposal.md，行為要求見 `specs/`。C-1 已經提供的、本 change 直接依賴的現況：

- `create_app()` 建立彼此獨立的實例；設定由 `load_config()` 驗證，機密包在 `Secret` 內；`init_request_logging` 在 `init_session` 之前註冊；請求日誌讀取 `g.user_id`（目前沒有人設定它）。
- 請求級 Session 在 `after_request` 於狀態碼小於 400 時提交，`teardown_request` 關閉；Repository 只 `flush`，不提交（`app/db/session.py`、`app/repositories/base.py`）。
- 層級掃描（`tests/architecture/test_layering.py`）：Controller 不得匯入 `app.repositories`／`app.db`／`app.models`／`sqlalchemy`；Service 不得匯入 `flask`；`services/registry.py` 是 Session → Repository → Service 的組裝處。
- `normalize_unique_text()`、`unique_when_not_null()`、`PublicIdMixin`、`TimestampMixin`、`UTCDateTime` 已存在。`.env` 只在 development 載入。
- 前端：`base.html` 已載入 Vue、axios、`UI`（Modal、Toast、Typewriter）；掃描測試禁止 `v-html`、`innerHTML`、`data-bs-*`（`theme` 除外）、外部網址。
- 本機 SQL Server 只監聽 `127.0.0.1`；測試用 `project_test`（`pytest -m sqlserver`）。

## Goals / Non-Goals

**Goals:**
- 所有安全相關的決定（雜湊、cookie、CSRF、權杖、Google 驗證、合併規則）集中在少數幾個可單獨測試的模組，後續 change 只需呼叫，不需要重新理解。
- 「不洩漏帳號是否存在」與「撤銷登入狀態」是結構性的保證（回應內容一致、並行安全、`token_version`），不是靠各端點自律。
- 所有流程都能在沒有真實 Google、沒有真實 SMTP 的環境下被自動測試完整驗證。

**Non-Goals:**
- 不做速率限制（IP 層屬入口層、每分鐘訊息數屬 C-4）；唯一例外是與帳號綁定、存放於資料庫的登入鎖定與寄信頻率。
- 不做 Google-only 帳號在 `/account` 的「重新驗證後設定密碼」：這類帳號在信箱驗證開啟時可用忘記密碼流程設定密碼（見 D7）。
- 不做變更 email、多因素驗證、條款改版後的重新同意、CSP 等安全標頭、帳號刪除與匯出（C-4）。

## Decisions

### D1. 模組佈局：新增 `app/security/` 與 `app/runtime.py`

```
app/
  runtime.py              # 組裝層共用：取得設定／寄信器／Google 供應商／時鐘（透過 current_app.extensions）
  cli.py                  # flask 指令：create-admin
  security/               # HTTP 安全輔助：可匯入 flask，不可碰資料庫
    csrf.py               #   權杖簽發與檢查、Origin 檢查
    cookies.py            #   簽章的短效 cookie（CSRF、OAuth 交易）
    session.py            #   登入 cookie 讀寫、current_user()、login_required_*
    redirects.py          #   next 轉址檢查
    errors.py             #   HttpSecurityError（403 csrf_failed 等）
  models/user.py          # User、UserIdentity、AuthToken 與狀態常數
  repositories/           # user_repository.py、user_identity_repository.py、auth_token_repository.py
  services/               # errors.py、auth_service.py、email_flow_service.py、google_sign_in_service.py、
                          # access_policy.py、password_hasher.py、mail_sender.py、mail_messages.py、
                          # google_oidc.py、legal.py（條款版本常數）
  controllers/            # auth_api_controller.py（/api/auth/*）、auth_page_controller.py、legal_controller（併入 page）
  templates/auth/、templates/legal/、static/js/auth/、static/css/auth.css
```

層級掃描新增 `security` 一列，禁止匯入規則同 `controllers`（`app.repositories`、`app.db`、`app.models`、`sqlalchemy`）。`runtime.py` 與 `cli.py` 和 `app/__init__.py` 同屬組裝層，不受限制。Service 仍不得匯入 `flask`：SMTP（標準函式庫 `smtplib`）與 Google 的 HTTP 呼叫（標準函式庫 `urllib`）都不需要它。

**替代方案**：把 cookie 與 CSRF 放進 `controllers/`——兩者會被所有 Blueprint 共用，放在 controllers 下語意不對，且 `login_required` 會被後續每個 Blueprint 匯入。

### D2. 資料模型與 migration `0002`（只新增三張表，符合 expand）

`users`（`PublicIdMixin`、`TimestampMixin`）

| 欄位 | 型別與約束 | 說明 |
|---|---|---|
| `id` | Integer 主鍵 | 不對外 |
| `email` | Unicode(255)，NOT NULL，唯一 | 小寫、NFC |
| `email_canonical` | Unicode(255)，NOT NULL，唯一 | 重複判斷用（D3） |
| `display_name` | Unicode(50)，NOT NULL | 不唯一 |
| `password_hash` | Unicode(255)，可為空 | 純 Google 帳號為空 |
| `email_verified_at` | UTCDateTime，可為空 | |
| `role`、`member_tier`、`status` | Unicode(10／10／20)，NOT NULL | 預設 `user`／`free`；`status` 由 Service 依模式設定 |
| `failed_login_count`、`locked_until` | Integer NOT NULL 預設 0／UTCDateTime 可為空 | |
| `token_version` | Integer，NOT NULL，預設 1 | |
| `terms_version`、`terms_accepted_at` | Unicode(40)／UTCDateTime，可為空 | 註冊與 Google 建立帳號時由 Service 必填；管理員指令建立的帳號為空（沒有「同意」這個動作） |
| `last_login_at` | UTCDateTime，可為空 | |

`user_identities`：`id`、`user_id`（外鍵，`ON DELETE CASCADE`）、`provider`（Unicode(20)）、`provider_subject`（Unicode(255)）、`created_at`；唯一約束 `(provider, provider_subject)` 與 `(user_id, provider)`。欄位皆 NOT NULL，因此不需要 `unique_when_not_null`。

`auth_tokens`：`id`、`user_id`（外鍵，`ON DELETE CASCADE`）、`purpose`（`verify_email`／`reset_password`）、`token_hash`（Unicode(64)，唯一）、`created_at`、`expires_at`、`used_at`（可為空）；索引 `(user_id, purpose, created_at)` 供寄信頻率查詢。

取捨：
- **不加資料庫 CHECK 約束**表達 `role`／`status` 值域，值域由 Service 常數與測試守住。CHECK 在 SQLite 的 batch migration 與 SQL Server 之間有方言差異，收益有限。
- **不放 `deleted_at`、`deletion_requested_at`**：刪除帳號屬 C-4，屆時以 expand 方式新增可為空欄位；`status` 的 `deletion_pending` 值先存在，登入時一律拒絕。
- 未建立 `public_id` 於 `user_identities`／`auth_tokens`：它們不對外暴露。
- 時間一律用既有的 `UTCDateTime`；比較在 Python 端進行，避免兩種資料庫的時間函式差異。

### D3. email 正規化：兩個函式，Service 層呼叫

`normalize_unique_text()` 已存在。新增 `canonical_email()`：先正規化；若網域為 `gmail.com` 或 `googlemail.com`，本機部分去除所有「.」並截掉第一個「+」之後的內容，網域統一為 `gmail.com`；其他網域不變。註冊與 Google 登入共用，使兩條路徑對「同一個人」的判斷一致。email 格式採保守的 ASCII 規則（本機部分英數與 `._%+-`，網域為標籤以點分隔，總長 ≤ 254），不做 DNS 檢查；非 ASCII 的地址在 v1 拒絕，避免 SMTPUTF8 與 IDN 的相容問題。

**替代方案**：`email-validator` 套件——多一個相依，且它預設接受的國際化地址我們反正要拒絕。

### D4. 密碼：Argon2id、NFKC、時間一致

- 使用 `argon2-cffi` 預設的 `PasswordHasher`（RFC 9106 的低記憶體設定，高於 OWASP 最低建議）；參數放在單一常數處。登入成功時以 `check_needs_rehash` 判斷並升級。
- 密碼先做 NFKC 正規化再雜湊／驗證（NIST SP 800-63B 建議）；長度以正規化後的字元數計，10 至 128。**此規則一旦上線就不能改**，否則既有雜湊失效，因此在這裡定案。
- 時間一致：登入時，信箱不存在、帳號沒有密碼、帳號被鎖定三種情況都對一個啟動時產生的「假雜湊」做一次驗證並忽略結果，使耗時與真實的驗證相當。註冊時，不論信箱是否已存在，都先雜湊密碼再判斷。

### D5. 登入狀態：Flask 簽章 session，只放 `uid` 與 `tv`

- 使用 Flask 內建的簽章 session cookie（`itsdangerous`），不引入額外套件。設定：`SESSION_COOKIE_HTTPONLY`、`SameSite=Lax`、`PERMANENT_SESSION_LIFETIME=7 天`、`SESSION_REFRESH_EACH_REQUEST=False`（到期時間固定在登入當下，不隨使用延長——「存活 7 天」取絕對期限）；staging／production 加 `Secure` 並將名稱設為 `__Host-session`（`__Host-` 要求 `Secure`、`Path=/`、無 `Domain`，三者皆已符合）。
- 登入時先 `session.clear()` 再寫入 `uid`、`tv`，避免沿用登入前的內容。
- `current_user()` 為**惰性**：只有端點或 `login_required_*` 呼叫時才讀資料庫，因此 `/healthz`、靜態檔不會因 cookie 而碰資料庫。它呼叫 `AuthService.resolve_session(uid, tv)`：使用者不存在、`token_version` 不符、或狀態為 `suspended`／`deletion_pending` 時回傳空值並清除 cookie；成功時在 `g` 設定 `user_id`（供既有的請求日誌使用）。
- 每次辨識都多一次主鍵查詢，是撤銷能力的代價；若日後成為瓶頸，再於 C-4 之後評估（但不得改用行程內快取，違反多 worker 規範）。

### D6. CSRF：自製「簽章雙重提交 cookie」加 Origin 檢查

- 權杖是 `itsdangerous` 簽章的隨機值，存在獨立的 cookie（`__Host-csrf`／`csrf`，HttpOnly、`SameSite=Lax`），同一個值由 `base.html` 放進 `<meta name="csrf-token">`。非 GET 請求要求 `X-CSRF-Token` 標頭等於 cookie 值（`hmac.compare_digest`）且簽章有效。
- 為什麼不放進 session cookie：AGENTS.md 規定登入 cookie 內容只含 `user_id` 與 `token_version`；而登入與註冊頁面在沒有 session 的情況下也需要權杖。
- 為什麼不用 Flask-WTF：它的權杖綁定 session，且我們沒有 WTForms 表單；它的 Referer 檢查規則與我們要的 Origin 規則不同，兩者併用反而難以推論。自製約 80 行，容易完整測試。
- Origin 檢查：預期來源取 `request.host_url`（staging／production 經 ProxyFix 已是對外的 https 網址）；`Origin` 缺少時用 `Referer` 的來源，兩者皆缺或不符（含 `null`）一律拒絕。
- 登入成功與登出時重新簽發權杖（舊權杖作廢）；前端因為整頁導向，會取得新的 meta 值。
- 前端 `static/js/auth/auth-common.js` 在載入時設定 `axios.defaults.headers.common['X-CSRF-Token']` 與預設逾時 15 秒；收到 `csrf_failed` 時以 Toast 提示重新整理。

### D7. 註冊、登入與通用回應的流程

註冊（`AuthService.register`）：驗證輸入 → 雜湊密碼 → 以 `email_canonical` 查詢 →
- 不存在：建立使用者（`status` 依 `EMAIL_VERIFICATION_REQUIRED`：開啟為 `pending_verification`，關閉為 `active`），開啟時發放驗證權杖並登記寄信；
- 存在且信箱未驗證且驗證開啟：**不覆蓋既有密碼**，只重新發放權杖並登記寄信（受頻率限制）；
- 其他：什麼都不做。
三種結果回傳同一個物件，Controller 回應同一個訊息。**不自動登入**（否則存在與不存在的情況回應會不同）。

為什麼不覆蓋待驗證帳號的密碼：若允許「最後註冊者勝出」，攻擊者可以在受害者註冊後覆蓋密碼，受害者點驗證信後帳號卻是攻擊者的密碼，且受害者毫無察覺。保留原密碼時，若攻擊者先佔用，受害者驗證後會發現密碼錯誤，改走忘記密碼（成功後 `token_version` 加 1、攻擊者的 session 失效，且密碼被受害者的新密碼取代）即可自癒。此殘餘風險見「Risks」。

並行註冊同一信箱：唯一索引使第二個寫入拋出完整性錯誤。`UserRepository.add_user` 在 `begin_nested`（SAVEPOINT）內 flush 並將其轉為 `DuplicateUserError`，Service 把它當作「存在」處理。SQLite 的 SAVEPOINT 需要 pysqlite 的交易設定，列為實作任務的驗證點（若不穩，改為 Repository 於此錯誤時 rollback 並重新查詢）。

登入（`AuthService.login`）：正規化信箱 → 查詢 → 套用鎖定規則（D8）→ 驗證密碼（時間一致，D4）→ 依 `status` 決定結果：
- 密碼錯誤、信箱不存在、無密碼、鎖定中 → 同一個 401 通用訊息；
- 密碼正確但 `suspended`／`deletion_pending` → 403「此帳號已停用」（只有持有正確密碼者看得到，不構成列舉）；
- `active`／`pending_verification` → 成功。

### D8. 鎖定：原子遞增

登入失敗用 ORM 的 `update(User).values(failed_login_count=User.failed_login_count + 1)` 一次完成遞增（兩個 worker 並行失敗不會互相覆蓋），再讀回計數；達 5 次時以條件式 UPDATE 設定 `locked_until = now + 15 分鐘`。進入登入流程時，若 `locked_until` 已過，先以條件式 UPDATE 歸零計數與鎖定（開啟新的計數週期）。鎖定中的嘗試不遞增、不延長。成功時歸零。

**失敗的計數必須在 401 之後仍然存在**（實作時由端到端測試發現）：請求級交易在狀態碼 400 以上預設回復，登入失敗回 401，失敗次數的 UPDATE 會隨之被丟掉，帳號永遠不會被鎖定；Service 層的測試因為不經過 HTTP 的提交規則而看不到這個問題。解法是 `app/db/session.py` 提供 `keep_changes_on_error()`：記錄失敗後呼叫，要求這個請求即使以 4xx 結束也提交；5xx 與未處理的例外一律仍回復。`AuthService` 以注入的 `persist_failures` 回呼使用它（Service 不匯入 flask），變更密碼時輸入錯誤的目前密碼走同一條路徑。這個例外只允許用在「變更本身就是失敗的記帳」。

取捨：攻擊者可以每 15 分鐘對受害者帳號鎖定一次（拒絕服務）。這是 AGENTS.md 指定的行為，緩解靠入口層的 IP 限流（C-5）；本 change 不另做。

### D9. 權杖：片段、雜湊、原子消耗、資料庫頻率

- 權杖 = `secrets.token_urlsafe(32)`；資料庫存其 SHA-256 十六進位。連結形如 `{BASE_URL}/verify-email#token=…`、`{BASE_URL}/reset-password#token=…`。權杖放在片段而不是查詢字串，是因為伺服器與入口層（Cloud Run）的存取日誌會記錄含查詢字串的完整網址，而 AGENTS.md 第 11 節禁止記錄含權杖的網址；片段不會被送出，也不會經 Referer 外洩。GET 回傳的確認頁因此天然不含權杖，也就天然滿足「GET 只顯示確認頁」。
- 消耗：`UPDATE auth_tokens SET used_at = :now WHERE token_hash = :h AND purpose = :p AND used_at IS NULL AND expires_at > :now`（以 ORM 的 `update()` 表達），以 `rowcount == 1` 判定成功，之後才讀取 `user_id`。並行兩個請求最多一個成功，不需要鎖。
- 發放新權杖時，同一使用者同用途的未使用舊權杖以條件式 UPDATE 設為已使用。
- 頻率限制：同一使用者同用途，最新一筆 `created_at` 距今不足 60 秒、或過去一小時內已發放 5 筆 → 不寄信。忘記密碼與註冊情境下靜默略過（回應不變）；已登入者的「重寄」回 429。計數來自資料表本身，不需要額外的限流表，也符合多 worker 規範。
- 清理過期權杖（到期後 7 天）屬 C-4 的排程指令。

### D10. 寄信與「提交後動作」

- `MailSender` 介面：`send(to, subject, body)`。實作 `SmtpMailSender`（`smtplib`，逾時 5 秒，STARTTLS 或 SSL，必要時登入）；測試使用記錄用的假實作。信件以 `email.message.EmailMessage` 建立，`utf-8` 內容與標題，收件人地址含 CR／LF 時拒絕。信件文案是 `mail_messages.py` 內的繁體中文常數與 `str.format`（不使用 Jinja，避免把使用者提供的顯示名稱當成範本處理）。
- 連結一律由設定的 `BASE_URL` 組成，不使用 `request.host`（主機標頭注入會讓重設連結指向攻擊者的網站）。
- **延後動作**：`app/db/session.py` 新增 `defer_until_commit(fn)`。Service 透過 `runtime` 取得它（Service 不匯入 flask）。`after_request` 在提交成功後，把已登記的動作掛到 `response.call_on_close`——也就是**回應送出之後**才執行：(a) 交易已提交；(b) 寄信不增加回應時間，註冊與忘記密碼對「帳號存在／不存在」的回應時間不會因為有沒有寄信而不同（關閉列舉的時間側通道）；(c) 此時請求與 Session 都已關閉，因此動作在登記時就必須把所需資料（收件人、內容、請求識別碼）綁好，不得再使用 `g`、`current_app` 或資料庫。每個動作各自 try／except，失敗只記錄錯誤類型。
- 後果：gunicorn 的執行緒在回應之後仍會忙碌至多 5 秒。**Cloud Run 預設在回應後節流 CPU**，可能讓背景寄信停滯；C-5 部署時須選擇「CPU 一律配置」或改用佇列，已列入 Open Questions。
- 測試：測試用的 client 以 `buffered=True` 開啟請求（會呼叫 `close`，觸發延後動作），由共用 fixture 提供。
- **替代方案**：回應前同步寄信——持有交易與可能卡住 5 秒，且有時間側通道；背景執行緒——在 Cloud Run 上與節流有同樣問題但更難測；任務佇列——v1 沒有基礎設施。

### D11. Google 登入：自行組裝 OIDC 授權碼流程

- 不使用 Authlib：它的 Flask 整合把 `state`／`nonce` 存進 Flask session，與「登入 cookie 只含 `uid`、`tv`」衝突，也會多帶一個大型相依。改用 `PyJWT[crypto]`（簽章驗證）加標準函式庫 `urllib`（5 秒逾時的 token 交換與 JWKS 取得）。
- 發起：`POST /api/auth/google/start`（受 CSRF 保護，帶 `accept_terms`）產生 `state`、`nonce`、PKCE `code_verifier`（S256），寫入獨立的簽章 cookie（`__Host-oauth_tx`／`oauth_tx`，10 分鐘、HttpOnly、`SameSite=Lax`——回呼是從 Google 回來的頂層 GET 導覽，`Lax` 會帶上），回傳授權網址由前端導向。這個 cookie 在回呼結束時（成功或失敗）一律刪除，因此同一個回呼網址第二次使用會失敗。
- 回呼：`GET /auth/google/callback`（頁面 Controller，回傳重新導向而非 JSON）。順序：驗證 `state`（`compare_digest`）→ 以 `code`＋`code_verifier` 向 Google 換取 token → 驗證 ID token（只接受 RS256；金鑰由 Google 的 JWKS 以 `kid` 選取；`iss` 為 `https://accounts.google.com` 或 `accounts.google.com`；`aud` 等於用戶端識別碼；`exp` 容許 60 秒時鐘誤差；`nonce` 一致；`sub`、`email` 存在；`email_verified` 必須是布林真）→ 交給 `GoogleSignInService`。
- JWKS 的記憶體快取只是效能最佳化（公開資料、可重建），不屬於會讓多 worker 行為分歧的「狀態」；快取失效時重新抓取。
- `GoogleSignInService` 依序處理：已綁定身分 → 直接登入（不因信箱變更而重新比對）；否則以 `email_canonical` 找本機帳號 → 已驗證則綁定；未驗證則清除密碼憑證、設為已驗證（待驗證轉 `active`）、`token_version` 加 1、綁定；沒有帳號則建立（需要發起時勾選同意）。本機帳號已綁定另一個 Google `sub` 時拒絕。
- 與 Google 的互動隔在 `GoogleIdentityProvider` 介面之後（`build_authorization_url`、`exchange_code`）：Service 測試用假供應商；真實實作的驗證邏輯另以「測試產生的 RSA 金鑰簽發 token＋假傳輸層」覆蓋全部拒絕情境。
- 錯誤一律以 `/login?error=<代碼>` 導回登入頁，代碼是固定列舉（`google_state`、`google_unavailable`、`google_rejected`、`terms_required`、`account_disabled`），頁面把代碼轉成繁體中文訊息；不把任何外部提供的文字放進網址或頁面。

### D12. 權限：`AccessPolicy`、業務錯誤與 IDOR 測試框架

- `services/errors.py`：`ServiceError` 及子類別 `ValidationError`（帶欄位說明）、`Unauthenticated`、`Forbidden`、`NotFound`、`Conflict`、`TooManyRequests`。`controllers/errors.py` 註冊一個處理器把它們對應為 400／401／403／404／409／429 與繁體中文 JSON（頁面則為錯誤頁），並補上 415 的訊息。
- `AccessPolicy` 是無狀態的類別：`is_admin(user)`、`is_owner(user, owner_id)`、`require_owner(user, owner_id)`（不是擁有者時拋 `NotFound`，不是 `Forbidden`）、`can_use_chat(user, verification_required)`。`member_tier` 不出現在任何方法中。
- `tests/support/idor.py`：`check_endpoint(app, spec)`，`spec` 描述方法、路徑樣板、如何為擁有者建立資源，以及請求內容；框架以擁有者、他人、不存在的 UUID、未登入四種身分各呼叫一次並斷言（擁有者成功、他人與不存在的 404 且回應本文逐字相同、未登入 401）。自我驗證測試使用一個測試專用的 Blueprint：一個正確、一個故意漏掉擁有者檢查的端點，後者必須被框架抓出。
- `flask create-admin`：`click` 指令，密碼以 `click.prompt(hide_input=True, confirmation_prompt=True)` 取得（非互動環境可由標準輸入提供），呼叫 `AuthService.create_admin`（驗證與雜湊規則同註冊），不接受密碼參數，不記錄密碼與 email。這個指令不經過 Flask 請求，因此 `services/registry.py` 增加以明確 Session 建構的 `build_*` 函式，指令自行提交。

### D13. 前端：每頁一支腳本，共用一支 `auth-common.js`

- 版型：`templates/auth/auth_base.html` 繼承 `base.html`，提供置中卡片、`<ui-toast-region>`，初始狀態以 `<script type="application/json" id="initial-state">{{ state|tojson }}</script>` 傳遞（`tojson` 會跳脫 `<`、`>`、`&`，可安全嵌入）。`base.html` 增加 `<meta name="csrf-token">`；各頁腳本載入 `auth-common.js` 後再載入自己的檔案。
- `auth-common.js`（UMD，與 `toast-store.js` 同樣可被 Node 測試）：設定 axios 預設值、`readInitialState()`、`readFragmentToken()`（讀取後以 `history.replaceState` 移除片段）、`describeError(error)`（把 axios 錯誤轉成繁體中文訊息與欄位錯誤；網路錯誤與逾時有各自的訊息）、401 時導向 `/login?next=…`（`next` 僅在伺服器端被驗證過才使用）。
- 表單狀態用 Vue `ref`，送出流程固定為 `submitting = true` → 呼叫 → `finally { submitting = false }`。錯誤訊息一律用 `{{ }}` 插值。
- 同意條款勾選與 Google 入口連動：Google 按鈕在未勾選時不發請求，直接提示（避免使用者被帶到 Google 才被拒絕）。
- `next` 檢查在伺服器端：必須是以單一 `/` 開頭、不含 `\`、控制字元、`//` 開頭的站內路徑，否則用預設頁面（目前是 `/account`；C-3 完成後改為 `/cs`，是單一常數）。

### D14. 日誌：新增 `event` 欄位

`ALLOWED_FIELDS` 加入 `event`。事件名稱是固定列舉（例如 `auth.login_failed.bad_password`、`auth.login_failed.unknown_user`、`auth.locked`、`auth.register`、`auth.email_verified`、`auth.password_reset`、`auth.google_login`、`auth.google_rejected.state`、`security.csrf_rejected`、`mail.send_failed`），失敗原因寫在事件名稱的最後一段，因此不需要再新增欄位，也不可能夾帶自由文字。已知使用者時帶 `user_id`；事件日誌不含 email、權杖、授權碼。這些名稱是將來「登入失敗激增」告警（AGENTS.md 第 11 節）的資料來源。

### D15. 設定：新增項目與驗證矩陣

| 變數 | development | testing | staging | production |
|---|---|---|---|---|
| `EMAIL_VERIFICATION_REQUIRED` 未設定時 | false | false | true | true |
| 可設為 false | 是 | 是 | 是 | **否（啟動失敗）** |
| `BASE_URL` | 驗證或 Google 啟用時必填 | 同左 | 必填且 https | 必填且 https |
| `MAIL_*`（主機、埠、寄件者） | 驗證開啟時必填 | 同左 | 同左 | 必填 |
| 寄信連線不加密 | 允許 | 允許 | 禁止 | 禁止 |
| `SECRET_KEY` 長度 | 不限制 | 不限制 | ≥ 32 | ≥ 32 |
| Google 用戶端識別碼與密鑰 | 兩者皆有或皆無（選用） | 同左 | 同左 | 同左 |

`AppConfig` 新增 `email_verification_required`、`base_url`、`mail`（`MailSettings`，密碼為 `Secret`）、`google`（`GoogleSettings`，密鑰為 `Secret`）、`secret_key_fallbacks`。`create_app()` 增加僅供注入的關鍵字參數（`mail_sender`、`identity_provider`、`clock`），預設從設定建立真實實作。本機 `.env` 不需修改即可啟動（development 預設為關閉驗證）。

### D16. 測試策略

- **Service／Repository 測試**以 `db_engine` fixture 同時跑 SQLite 與 `sqlserver` 標記（`project_test`）：唯一性（email、canonical、`provider_subject`）、外鍵與 `ON DELETE CASCADE`、原子遞增與原子消耗（用兩條連線模擬並行）、中文顯示名稱往返。
- **整合測試**使用 Flask test client，輔助函式自動帶 Origin 與 CSRF 標頭；時間使用注入的 `FakeClock`（權杖過期、鎖定到期、七天 cookie 不必真的等待）；寄信使用記錄用的假寄信器；Google 使用假供應商，另有真實供應商實作的驗證測試。
- **SMTP**：以 `aiosmtpd` 在執行緒內啟動本機伺服器驗證真實的 `SmtpMailSender`（UTF-8 標題與內文、逾時、失敗不拋出到請求）。STARTTLS／SSL 路徑以替身驗證呼叫順序，真實 TLS 屬人工驗收。
- **日誌掃描**：跑完整流程後，以固定的 email、密碼、權杖樣本掃描全部輸出，必須找不到。
- **架構測試**：層級掃描新增 `security`；前端掃描自動涵蓋新樣板與腳本；原生 SQL 掃描涵蓋新 Repository 與 migration。
- **Node 單元測試**：`auth-common.js`（片段讀取、錯誤描述、`next` 防呆的前端部分）。
- **瀏覽器驗收**（Edge CDP，沿用 C-1 方法）：註冊→登入→帳號頁→登出、錯誤情境、360／1280 寬度、鍵盤操作。真實 Google 與真實信箱到達屬人工驗收，列為上線前關卡。

### D17. 新增相依套件

`argon2-cffi`（Argon2id）、`PyJWT[crypto]`（連帶 `cryptography`）；開發用 `aiosmtpd`。實作第一步先確認三者在 Python 3.14／Windows／`python:3.14-slim-bookworm` 皆有預編譯版本並釘死版本，不然改用相容的替代方案再回報。

## Risks / Trade-offs

- [預先佔用信箱（pre-hijacking）：攻擊者先以受害者的信箱註冊，受害者點了驗證信] → 保留原密碼而非覆蓋；受害者發現密碼不符後走忘記密碼（會使攻擊者的登入狀態失效並取代密碼）；Google 登入合併時清除未驗證的密碼憑證並使 session 失效；信件文案寫明「若非本人操作請忽略」。**殘餘風險**：受害者點了信、也不察覺而繼續使用帳號時，攻擊者仍可登入——這是「先註冊後驗證」模式的固有限制，完整解法（驗證時才設定密碼）會改變 AGENTS.md 指定的註冊流程，不在此 change 處理。
- [登入鎖定可被用於阻斷特定帳號] → 這是 AGENTS.md 指定的行為；C-5 以入口層 IP 限流緩解。
- [信箱驗證關閉的 staging：帳號未驗證、Google 合併規則會清除其密碼] → 依 report 4.4 的既定預設；README 與 `.env.example` 註明；正式環境不會出現。
- [`session` 每次請求一次主鍵查詢] → 換取可撤銷；日後以資料庫層最佳化處理，不使用行程內快取。
- [SQLite 的 SAVEPOINT 行為（pysqlite）] → 實作時以測試驗證；不穩則改用 rollback 後重查。
- [JWKS 取得依賴 Google 的網路可用性] → 5 秒逾時、失敗時導回登入頁並顯示服務暫時無法使用；密碼登入不受影響。
- [Argon2id 約 64 MiB／次的記憶體] → 每個 worker 同時最多 8 個執行緒登入，約 0.5 GiB 峰值；C-5 依實際容器記憶體與壓測調整參數（單一常數）。
- [回應後寄信在 Cloud Run 預設可能被節流] → 見 D10 與 Open Questions。
- [條款與隱私權政策為草稿] → 頁面明確標示草稿；法務覆核屬上線前關卡。
- [`gmail.com` 去點規則誤判] → 只對 Gmail／Googlemail 啟用，這是 AGENTS.md 指定的範圍。

## Migration Plan

1. 備份開發資料庫 `project` 後，執行 `alembic upgrade head`（套用 `0002`，只新增三張表，不動既有結構）；`project_test` 同樣升級。
2. 部署順序遵循 AGENTS.md：migration 先於程式；`0002` 對舊程式無影響（舊版程式不會讀這些表）。
3. 回復：`alembic downgrade 0001`（刪除三張表；此時尚無正式資料）。`0002` 之後若已有使用者資料，不可 downgrade，需改用備份還原。
4. 本機 `.env` 不需修改；若要在本機測試信箱驗證，需新增 `EMAIL_VERIFICATION_REQUIRED=true`、`BASE_URL` 與 `MAIL_*`，並執行 `scripts/dev_smtp_sink.py` 擷取信件。

## Open Questions

- Cloud Run 的 CPU 配置（回應後寄信是否需要「CPU 一律配置」，或改用任務佇列）——C-5 部署時依實測決定；不影響本 change 的規格與任務。
- 正式環境的寄信服務供應商（SES、Mailgun、SendGrid 等）與 SPF／DKIM／DMARC——上線前關卡；程式只依賴標準 SMTP 設定。
- 開發用的 Google OAuth 用戶端（Google Cloud Console 建立，授權的重新導向網址為 `http://localhost:5000/auth/google/callback`）——人工驗收真實流程時才需要，由使用者準備。
- C-3 完成後將登入後的預設頁面由 `/account` 改為 `/cs`（單一常數）。
