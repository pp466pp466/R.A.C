# Tasks

> 實作順序即依賴順序。每組都自帶它需要的測試與文件；最後一組只做跨組整合驗收。
> 全程遵守：機密（資料庫密碼、Gemini 金鑰、Google 密鑰、SMTP 密碼）只存在已被 `.gitignore` 排除的 `.env` 或測試用的假值，不得出現在程式碼、設定範本、文件、日誌或對話中。
> 本機沒有 Windows 原生 `docker`：容器指令一律透過 `wsl -d Ubuntu-2204 -u root -- ...`，使用前先執行 `service docker start`。
> 測試輔助一律帶 `Origin` 與 CSRF 標頭並以 `buffered=True` 開啟請求（見 3.3），不要在個別測試重寫。

## 1. 相依套件與設定（app-configuration）

- [x] 1.1 確認 `argon2-cffi`、`PyJWT[crypto]`（含 `cryptography`）在 Windows／Python 3.14 與 `python:3.14-slim-bookworm`（WSL 內以 `pip download --only-binary=:all:` 驗證）皆有預編譯版本，釘死版本寫入 `requirements.txt`，`aiosmtpd` 寫入 `requirements-dev.txt`。驗證：`pip install -r requirements-dev.txt` 成功且 `python -c "import argon2, jwt, cryptography, aiosmtpd"` 無錯誤；若任一套件沒有預編譯版本，停下並回報再決定替代方案
- [x] 1.2 擴充 `app/config.py`：`email_verification_required`（依環境的預設與 production 禁止 false）、`base_url`（絕對來源、無路徑查詢片段、staging／production 須 https）、`MailSettings`（主機、埠、寄件者、帳號密碼須成對、`starttls`／`ssl`／`none`，`none` 僅 development 與 testing）、`GoogleSettings`（識別碼與密鑰須成對）、`SECRET_KEY` 在 staging／production 至少 32 字元、`SECRET_KEY_FALLBACKS`；新機密一律包 `Secret`。驗證：`tests/test_config.py` 新增的參數化測試涵蓋 app-configuration 與 google-sign-in 規格中的每個情境（含錯誤訊息只含變數名稱、不含值）並通過
- [x] 1.3 更新 `.env.example`（只含新變數名稱與繁體中文說明，註明本機建議 `EMAIL_VERIFICATION_REQUIRED=false`）與 `README.md` 的「環境變數與機密」表。驗證：既有的 `.env.example` 與 README 一致性測試通過，且新變數全部出現
- [x] 1.4 在 `create_app()` 套用 Flask 設定（session cookie 名稱／Secure／HttpOnly／SameSite=Lax／7 天／不隨使用延長、`SECRET_KEY_FALLBACKS`、`MAX_CONTENT_LENGTH` 為 64 KB），新增僅供注入的關鍵字參數 `mail_sender`、`identity_provider`、`clock`，並建立 `app/runtime.py`（取得設定、寄信器、供應商、時鐘；時鐘預設為 UTC 現在時間，寄信器與供應商的預設實作由設定建立，分別在 7.1 與 8.1 補上，因為那時才有對應的類別）。驗證：測試斷言 production 的 cookie 名稱以 `__Host-` 開頭並帶 `Secure`、development 不帶；注入的假實作被 `runtime` 回傳；既有的應用程式工廠測試全部通過

## 2. 資料模型、Repository 與 migration（member-registration、google-sign-in、email-token-flows）

- [x] 2.1 建立 `app/models/user.py`（`User`、`UserIdentity`、`AuthToken` 與角色／等級／狀態／用途常數，欄位與約束依 design D2），並讓 `app/models/__init__.py` 匯入全部模型、`migrations/env.py` 匯入 `app.models` 以註冊到 metadata。驗證：測試斷言 metadata 含三張表，且 `users.email`、`users.email_canonical`、`auth_tokens.token_hash`、`(provider, provider_subject)`、`(user_id, provider)` 皆有唯一約束
- [x] 2.2 在 `app/services/normalization.py` 新增 `canonical_email()` 與保守的 ASCII email 格式檢查（design D3）。驗證：單元測試涵蓋 member-registration 規格中的大小寫、gmail／googlemail 去點與去 `+標籤`、其他網域不去點、非 ASCII 與過長地址被拒絕
- [x] 2.3 實作 `UserRepository`、`UserIdentityRepository`、`AuthTokenRepository`：以 email／canonical 查詢、新增（重複時轉為 `DuplicateUserError`）、依主鍵取得（供辨識 session）、原子遞增失敗計數與條件式鎖定／解除、原子 `token_version + 1`、權杖發放／原子消耗（`used_at IS NULL AND expires_at > now` 的條件式 UPDATE 並以 rowcount 判定）／使同用途舊權杖失效／查詢最新發放時間與一小時內筆數。全程只用 ORM 表達式。驗證：以 `db_engine` fixture 在 SQLite 與 SQL Server 上跑的測試涵蓋唯一性（大小寫、canonical、`provider_subject`）、外鍵與 `ON DELETE CASCADE`、中文顯示名稱往返，並用兩條獨立連線證明並行失敗計數不被低估、並行消耗同一權杖只有一個成功；原生 SQL 掃描通過
- [x] 2.4 以 Alembic 產生 migration `0002`（僅 `op` API，不含原生 SQL；啟用 batch），清理自動產生的內容與命名。驗證：對全新檔案型 SQLite 與 `project_test` 執行 `upgrade head` 再 `downgrade 0001` 皆成功；測試以 Alembic 的 `compare_metadata` 斷言升級後的結構與模型沒有差異；原生 SQL 掃描對 migration 通過
- [x] 2.5 將開發用的 `project` 升級到 `0002`。驗證：`alembic upgrade head` 成功，`alembic current` 顯示 `0002`，`pytest -m sqlserver` 全數通過

## 3. 共用基礎：業務錯誤、請求本文、提交後動作、日誌事件、層級掃描

- [x] 3.1 建立 `app/services/errors.py`（`ServiceError` 與 `ValidationError`（含欄位說明）、`Unauthenticated`、`Forbidden`、`NotFound`、`Conflict`、`TooManyRequests`），並在 `app/controllers/errors.py` 註冊統一對應（400／401／403／404／409／429，補上 415 訊息；API 回 JSON，頁面回錯誤頁）。驗證：測試涵蓋 application-layering 規格的兩個業務錯誤情境，且回應不含堆疊追蹤與路徑
- [x] 3.2 在 Controller 側建立讀取 JSON 本文的共用函式：內容類型必須是 JSON（否則 415）、最外層必須是物件（否則 400）、本文超過 64 KB 回 413。驗證：測試涵蓋 application-layering 規格的三個本文情境
- [x] 3.3 在 `app/db/session.py` 實作 `defer_until_commit(fn)`：登記的動作在提交成功後掛到 `response.call_on_close`（依序執行、個別 try／except 只記錄錯誤類型），錯誤狀態碼或回復時丟棄；`runtime` 提供給 Service 使用；建立測試共用 fixture（`buffered=True` 的 client、自動帶 Origin 與 CSRF 標頭的輔助函式）。驗證：測試涵蓋 database-access 規格的三個延後動作情境，並證明動作執行時請求與 Session 已關閉（使用 `g` 或資料庫會失敗，故動作必須預先綁定資料）
- [x] 3.4 在 `ALLOWED_FIELDS` 加入 `event`，建立安全事件名稱列舉與 `log_event()` 輔助函式（只接受列舉內的名稱；已知使用者時帶 `user_id`）。驗證：測試涵蓋 service-observability 規格的「事件名稱欄位」情境，且不在列舉內的名稱被拒絕（避免自由文字進入日誌）
- [x] 3.5 層級掃描新增 `security` 一列（禁止匯入 `app.repositories`、`app.db`、`app.models`、`sqlalchemy`），並補上單元測試。驗證：application-layering 規格的「HTTP 安全輔助直接讀資料庫」情境有對應測試且通過，全專案掃描通過

## 4. 認證 Service、密碼與權限政策（member-registration、session-authentication、access-control）

- [x] 4.1 實作密碼雜湊模組：Argon2id（`argon2-cffi` 預設參數集中於一個常數處）、NFKC 正規化、長度 10 至 128 的檢查、`needs_rehash`、啟動時產生的假雜湊（供時間一致的驗證使用）。驗證：單元測試涵蓋長度邊界（9、10、128、129）、全形與半形等價、不強制字元組合、舊參數雜湊在登入後升級，以及儲存的字串確為 Argon2id 且不含明文
- [x] 4.2 實作 `AuthService.register`（驗證輸入、先雜湊、依 `email_canonical` 查詢、三條路徑、狀態依 `EMAIL_VERIFICATION_REQUIRED`、記錄條款版本與時間、並行重複時當作已存在處理、角色與等級欄位一律忽略）與條款版本常數 `legal.py`。驗證：測試涵蓋 member-registration 規格的每個情境，包含「兩種情況都呼叫一次雜湊」「已存在帳號的密碼與資料不變」「待驗證帳號重複註冊不覆蓋密碼」，並在兩種資料庫上驗證唯一性
- [x] 4.3 實作 `AuthService.login`：正規化、鎖定規則（鎖定中不遞增不延長、到期後開新週期、成功歸零）、時間一致的密碼驗證、依狀態決定結果（401 通用訊息、403 停用）、成功時更新最後登入時間並視需要重新雜湊。驗證：測試涵蓋 session-authentication 規格中「登入失敗回應一致」「連續失敗鎖定帳號」「帳號狀態對登入的影響」的全部情境，並以兩條連線驗證並行失敗不低估
- [x] 4.4 實作 `AuthService.resolve_session`（比對 `token_version` 與狀態）、`logout_all`、`change_password`、目前使用者資料物件（不含雜湊、內部編號與 `token_version`）。驗證：測試涵蓋「以 token_version 撤銷」「變更密碼」「查詢目前登入者」的情境，包含停權後下一次辨識失效、純 Google 帳號變更密碼被拒絕、目前密碼錯誤計入失敗次數
- [x] 4.5 實作 `AccessPolicy`（`is_admin`、`is_owner`、`require_owner` 不是擁有者時拋 `NotFound`、`can_use_chat`，會員等級不參與任何判斷）。驗證：單元測試涵蓋 access-control 規格的政策與「角色與會員等級的語意」情境，包含信箱驗證開啟／關閉兩種模式下 `can_use_chat` 的差異
- [x] 4.6 實作 `AuthService.create_admin` 與 `flask create-admin`（`app/cli.py`；密碼以不回顯方式輸入或讀標準輸入；不接受密碼參數；信箱重複時失敗且不改動既有帳號；`services/registry.py` 增加以明確 Session 建構的函式，指令自行提交）。在 `README.md` 補上使用方式。驗證：以 click 的 `CliRunner` 測試涵蓋 access-control 規格的建立與信箱重複情境，並斷言密碼不出現在輸出與日誌

## 5. HTTP 安全輔助：cookie、CSRF、登入狀態、轉址（csrf-protection、session-authentication）

- [x] 5.1 實作 `app/security/cookies.py`：簽章的短效 cookie 讀寫（支援 `SECRET_KEY_FALLBACKS`、可注入時鐘、`__Host-` 命名依環境）。驗證：單元測試涵蓋竄改、過期、舊金鑰驗證通過而新簽發使用新金鑰
- [x] 5.2 實作 `app/security/csrf.py`：權杖簽發（獨立 cookie 加 `<meta>` 值）、非 GET 一律檢查標頭與 cookie 一致且簽章有效、Origin／Referer 規則、登入與登出時輪替、`403 csrf_failed`，並在 `base.html` 加入 `<meta name="csrf-token">`；檢查失敗記錄 `security.csrf_rejected` 事件。驗證：測試涵蓋 csrf-protection 規格的全部情境，包含登入端點也需要權杖、自行編造的未簽章權杖被拒絕、權杖不出現在網址、staging 經 ProxyFix 的來源判斷
- [x] 5.3 實作 `app/security/session.py`：登入時 `session.clear()` 後寫入 `uid`、`tv`、登出清除、惰性的 `current_user()`（呼叫 `resolve_session`，失敗時清除 cookie，成功時設定 `g.user_id`）、`login_required_api`（401）與 `login_required_page`（導向 `/login?next=…`）。驗證：測試涵蓋 session-authentication 規格的 cookie 屬性、七天到期（假時鐘）、竄改、撤銷等情境，並證明帶著 cookie 請求 `/healthz` 時資料庫停止仍回 200
- [x] 5.4 實作 `app/security/redirects.py` 的 `safe_next()`。驗證：參數化測試涵蓋 auth-pages 規格的轉址情境，另含 `javascript:`、`/\evil`、`/%2f/evil`、含換行的值、過長的值
- [x] 5.5 為 `/api/auth/` 與含權杖的確認頁加上 `Cache-Control: no-store`。驗證：測試涵蓋 session-authentication 規格的「認證回應不可被快取」情境

## 6. 認證 API 與權限測試框架（session-authentication、member-registration、access-control）

- [x] 6.1 建立 `app/controllers/auth_api_controller.py` 的 `/api/auth/register`、`/login`、`/logout`、`/logout-all`、`/me`、`/change-password`（snake_case 欄位、欄位錯誤格式、成功登入後輪替 CSRF 權杖、不回傳內部編號與雜湊）並在 `create_app()` 註冊。驗證：端到端測試涵蓋 member-registration 與 session-authentication 規格的每個 API 情境
- [x] 6.2 補上列舉防護的對照測試：已存在與不存在信箱的註冊，以及不存在信箱、密碼錯誤、鎖定中三種登入，回應狀態碼與本文逐字相同。驗證：測試通過
- [x] 6.3 在註冊、登入、登出、登出所有裝置、變更密碼等流程記錄安全事件（失敗原因寫在事件名稱）。驗證：測試涵蓋 service-observability 規格的「登入失敗」「帳號被鎖定」情境，且日誌不含 email、密碼與 Cookie
- [x] 6.4 建立 `tests/support/idor.py`：`check_endpoint()` 以擁有者、他人、不存在的 UUID、未登入四種身分檢查端點，並附自我驗證測試（測試專用 Blueprint 內一個正確、一個故意漏掉擁有者檢查的端點）。驗證：access-control 規格的兩個框架情境通過；README 的「撰寫新端點」章節說明用法

## 7. 信箱權杖流程與寄信（email-token-flows）

- [x] 7.1 實作 `SmtpMailSender`（並在 `create_app()` 於設定了寄信參數且沒有注入時以設定建立它；`smtplib`、5 秒逾時、STARTTLS／SSL／不加密、需要時登入、收件人含 CR／LF 時拒絕）與 `mail_messages.py`（繁體中文純文字、UTF-8、說明有效期限與「若非本人操作請忽略」）。驗證：以 `aiosmtpd` 啟動本機伺服器的測試確認收到的標題與內文解碼後為正確的繁體中文；以替身驗證 STARTTLS／SSL 的呼叫順序；逾時與連線失敗不會拋出到請求；標頭注入被拒絕
- [x] 7.2 實作 `EmailFlowService`：發放（雜湊、取代舊權杖）、原子消耗、信箱驗證、重寄、忘記密碼、重設密碼、通知信、60 秒間隔與每小時 5 封的頻率限制；寄信全部透過 `defer_until_commit`，連結以 `BASE_URL` 組成並使用 `#token=` 片段。驗證：測試涵蓋 email-token-flows 規格的每個情境（以假時鐘驗證過期、兩條連線驗證並行消耗），並斷言資料庫沒有權杖原值
- [x] 7.3 建立 `/api/auth/verify-email`、`/resend-verification`、`/forgot-password`、`/reset-password`；信箱驗證關閉時一律 404；記錄對應的安全事件。驗證：測試涵蓋「信箱驗證關閉時停用相關功能」「提交成功後才寄信」「偽造主機標頭」情境，且重設權杖無法用於信箱驗證
- [x] 7.4 補上跨流程的攻擊情境測試：攻擊者預先註冊受害者信箱 → 受害者再次註冊（密碼不被覆蓋）→ 受害者走忘記密碼（攻擊者的 session 失效、舊密碼失效、信箱視為已驗證）；純 Google 帳號以重設流程設定密碼後兩種方式都能登入。驗證：測試通過
- [x] 7.5 建立 `scripts/dev_smtp_sink.py`（以 `aiosmtpd` 在本機收信並把信件印到終端機，僅供開發）。在 `README.md` 補上「本機測試信箱驗證」章節（所需 `.env` 變數、啟動順序）。驗證：依文件操作，從註冊到點開驗證連結（在終端機看到信件）完整走通一次，記錄於 README 的驗收紀錄

## 8. Google 登入（google-sign-in）

- [x] 8.1 實作 `google_oidc.py`（並在 `create_app()` 於 Google 已設定且沒有注入時以設定建立它）：授權網址（`openid email profile`、`state`、`nonce`、PKCE S256）、授權碼交換（`urllib`、5 秒逾時）、ID token 驗證（只接受 RS256、JWKS 依 `kid`、`iss`、`aud`、`exp`（60 秒誤差）、`nonce`、`sub`、`email`、`email_verified` 為布林真）。驗證：以測試產生的 RSA 金鑰與假傳輸層覆蓋 google-sign-in 規格 ID token 驗證的全部拒絕情境（信箱未驗證、受眾不符、nonce 不符、簽章無效、演算法非 RS256、過期、Google 無法連線）以及一個成功案例
- [x] 8.2 實作 `GoogleSignInService`：已綁定身分直接登入、以 canonical 信箱尋找本機帳號、已驗證則綁定、未驗證則清除密碼憑證＋視為已驗證＋`token_version` 加 1＋綁定、無帳號則建立（需同意條款並記錄）、已綁定另一個 `sub` 時拒絕、停權／刪除中拒絕、顯示名稱取 Google 名稱或信箱本機部分。驗證：測試涵蓋 google-sign-in 規格的對應與合併、同意條款、帳號狀態情境（含預先佔用信箱的攻擊情境），身分的唯一性在兩種資料庫上驗證
- [x] 8.3 建立 `POST /api/auth/google/start`（受 CSRF 保護，寫入簽章的 OAuth 交易 cookie）與 `GET /auth/google/callback`（驗證 `state`、處理後一律刪除交易 cookie、成功時建立與密碼登入相同的 session 並導向安全的 `next`、失敗時導向 `/login?error=<固定代碼>`）；未設定 Google 時兩者皆 404。驗證：以假供應商的整合測試涵蓋 google-sign-in 規格的授權碼流程防護（state 不符、沒有綁定、逾時、重複使用）與 Google 登入為選用功能的情境，並確認 `error` 代碼不夾帶外部文字

## 9. 前端頁面（auth-pages、member-registration）

- [x] 9.1 建立 `templates/auth/auth_base.html`、`static/css/auth.css`（沿用 `theme.css` 色彩與字型變數；表單、錯誤、載入狀態；360px 至桌面皆無水平捲軸）。驗證：既有前端規範掃描（樣板與自有 CSS／JS）通過
- [x] 9.2 建立 `static/js/auth/auth-common.js`（UMD；axios 預設值含 `X-CSRF-Token` 與 15 秒逾時、`readInitialState()`、`readFragmentToken()` 讀取後移除片段、`describeError()`、`csrf_failed` 提示、401 導向登入）。驗證：`tests/js/auth-common.test.js` 涵蓋片段解析、錯誤描述（網路／逾時／欄位錯誤）與 CSRF 失敗提示，Node 測試通過
- [x] 9.3 建立 `/login`、`/register` 頁面（Page Controller 路由、初始狀態 JSON、`finally` 還原、欄位錯誤、Google 入口與同意勾選連動、已登入導向 `/account`）與對應的 `login.js`、`register.js`。驗證：測試涵蓋 auth-pages 規格的頁面存取、安全轉址、驗證關閉時不顯示忘記密碼入口、Google 未啟用時不顯示入口
- [x] 9.4 建立 `/forgot-password`、`/reset-password`、`/verify-email` 頁面與腳本（權杖來自片段、載入時不自動送出、缺少權杖的說明、完成後的下一步連結）；信箱驗證關閉時 404。驗證：測試涵蓋 auth-pages 規格的確認頁情境（伺服器回傳的 HTML 不含權杖）
- [x] 9.5 建立 `/account`（顯示名稱、email、會員等級繁體中文標籤、信箱驗證狀態與重寄按鈕、變更密碼、登出、登出所有裝置；後兩者先以自製 Modal 確認；Vue 狀態接管 Modal）與 `account.js`。驗證：測試涵蓋 auth-pages 規格的帳號設定頁情境（需登入、等級標籤、待驗證提示）
- [x] 9.6 建立 `/terms`、`/privacy` 的繁體中文草稿頁（標示草稿與待法務覆核；隱私權政策依 AGENTS.md 第 11 節寫明對話傳送至 Gemini、各項保存期限與當事人權利），註冊頁連結兩者。驗證：測試涵蓋 member-registration 規格的兩個條款頁情境，並斷言頁面含「草稿」標示、Gemini 告知與保存期限表
- [x] 9.7 對全部新增樣板與前端檔案執行既有前端規範掃描，並以自製斷言確認新頁面的 `data-bs-*` 僅有 `data-bs-theme="dark"`、沒有 `v-html`／`innerHTML`、沒有外部資源。驗證：`tests/architecture/test_frontend_rules.py` 通過，且渲染後的各頁面都納入「只載入本站資源」的檢查清單

## 10. 整合驗收

- [x] 10.1 新增「完整流程後掃描日誌」測試：以固定的 email、密碼與權杖樣本跑過註冊、驗證、登入、忘記密碼、重設、Google 登入、登出，收集全部標準輸出。驗證：測試通過（對應 service-observability 規格的「完整流程後掃描日誌」）
- [x] 10.2 執行完整測試：`pytest`（SQLite）、`pytest -m sqlserver`（`project_test`）、`node --test "tests/js/**/*.test.js"`、`openspec validate member-auth --strict`。驗證：全部通過，並回報測試數量
- [x] 10.3 對開發用 `project`（SQL Server）以 `flask run` 啟動，用腳本（`urllib`＋cookie jar，含 CSRF 與 Origin）實際走過註冊（驗證關閉）→ 登入 → `/api/auth/me` → 變更密碼 → 登出所有裝置 → 舊 session 失效；另用 `flask create-admin` 建立管理員並確認可登入。驗證：每步回應符合預期，操作後清理測試資料
- [x] 10.4 瀏覽器驗收（Edge CDP，沿用 C-1 的做法）：註冊→登入→帳號頁→登出、錯誤密碼與欄位錯誤、鎖定訊息、鍵盤操作、360px 與 1280px 無水平捲軸、Modal 確認流程、驗證連結的片段被移除、Console 無錯誤。驗證：逐項結果記錄於 `README.md` 的驗收紀錄
- [x] 10.5 容器驗收：重建映像，擴充 `scripts/verify_container.py`（以容器內的 SQLite 執行 `alembic upgrade head` 後，經 HTTP 完成註冊、登入與 `/api/auth/me`；確認映像不含 `.env`）。驗證：透過 WSL 執行驗收腳本全數通過，映像仍以非 root 執行
- [x] 10.6 機密與規範掃描：對工作目錄與暫存區搜尋資料庫密碼、Gemini 金鑰、測試用假密鑰以外的長隨機字串；確認 `.env` 未被追蹤；`tests/test_agents_skill_sync.py` 仍通過。驗證：掃描結果為 0 筆，`git status` 沒有 `.env`
- [x] 10.7 更新文件：`README.md`（認證流程總覽、`EMAIL_VERIFICATION_REQUIRED` 兩種模式、`create-admin`、`dev_smtp_sink`、已知限制與驗收紀錄）、`docs/spec_report_v2.2.0.md` 新增紀錄 016（C-2 的設計決定與偏離）。驗證：README 一致性測試通過，文件中的指令可依序執行

## Workflow follow-up

- 人工驗收（不阻擋封存）：以開發用 Google OAuth 用戶端（使用者自行建立）走過真實的 Google 登入與合併；以真實信箱確認驗證信的到達與顯示。這兩項最終屬上線前關卡（C-5）。
- 使用者確認驗收結果後，封存此 change（`/openspec-archive-change member-auth`），並在 `docs/report.md` 4.1 節補記 C-2 完成。
