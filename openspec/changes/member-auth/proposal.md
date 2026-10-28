# Proposal

## Why

C-1 已經把地基準備好（設定、雙軌資料庫、分層、離線前端、日誌、容器），但系統還沒有「人」：沒有使用者資料表、沒有登入，也沒有任何可以判斷「這份資料是誰的」的機制。後續的客服對話（C-3）、額度與後台（C-4）都必須先知道使用者是誰，並保證使用者只能碰到自己的資料；帳號與權限若在客服功能寫完之後才補，每個端點都得回頭改，也容易漏掉擁有者檢查。因此在任何對話資料出現之前，先把會員驗證、CSRF、資料隔離的政策物件與測試框架定案。

## What Changes

- 建立使用者相關三張表與 migration：`users`（email 與 `email_canonical` 各自唯一、`display_name` 不唯一、`role`、`member_tier`、`status`、登入失敗計數與鎖定時間、`token_version`、同意條款紀錄）、`user_identities`（Google 身分）、`auth_tokens`（只存雜湊的驗證／重設權杖）。
- 信箱加密碼註冊：勾選同意服務條款與隱私權政策並記錄版本與時間；email 正規化為小寫，gmail.com 與 googlemail.com 另去除「.」與「+標籤」後比對；註冊、忘記密碼與登入失敗一律回應相同的通用訊息，不洩漏帳號是否存在。
- 密碼以 Argon2id 雜湊（長度 10 至 128 字，不強制字元組合）；登入採簽章 session cookie（內容只含 `user_id` 與 `token_version`，不使用 JWT）；改密碼、登出所有裝置、重設密碼、綁定 Google 時 `token_version` 加 1 以撤銷既有 session。
- 同一帳號連續登入失敗 5 次鎖定 15 分鐘，計數存放於資料庫。
- 信箱驗證與重設密碼：32 位元組隨機權杖、資料庫只存 SHA-256 雜湊、單次使用（驗證 24 小時、重設 1 小時失效）；連結的 GET 只顯示確認頁，由 POST 完成動作；以 SMTP 寄出 UTF-8 繁體中文信件，並於交易提交後才寄送。
- 新增 `EMAIL_VERIFICATION_REQUIRED`：development、testing、staging 可為 `false`（註冊後直接 `active`、`email_verified_at` 保持空值、忘記密碼與寄信一併停用）；production 必須為 `true` 並完成寄信設定，否則啟動失敗。
- Google 登入：Authorization Code 流程搭配 PKCE、`state`、`nonce`；驗證 ID token 的簽章、`iss`、`aud`、`exp`、`nonce` 並要求 `email_verified` 為真；同信箱帳號合併規則（以 `email_verified` 為前提，清除未驗證的密碼憑證並撤銷其 session）。Google 設定為選用，未設定時隱藏入口。
- CSRF：所有非 GET 請求須帶 `X-CSRF-Token` 並驗證 `Origin`；axios 預設帶標頭。
- 資料隔離：集中的 `AccessPolicy`（擁有者、管理員判斷）、非擁有者一律 404、用於後續 change 的參數化 IDOR 測試框架；以一次性 `flask` 指令建立首位管理員。
- 前端頁面：`/login`、`/register`、`/forgot-password`、`/reset-password`、`/verify-email`、`/account`（改密碼、登出所有裝置、顯示會員等級標籤）、`/terms`、`/privacy`（繁體中文草稿，標示待法務覆核），沿用 C-1 的 Modal、Toast，Vue 全域變數模式。
- 擴充既有規格：設定（驗證開關、`BASE_URL`、寄信、Google、`SECRET_KEY` 強度與輪替）、層級依賴（新增 HTTP 安全輔助層、Service 錯誤統一對應 HTTP 狀態）、資料庫工作階段（提交後才執行的動作）、日誌（安全事件名稱欄位）。
- 不在範圍內：客服對話與 Gemini（C-3）、額度與速率限制、管理員 Web 後台、刪除帳號與資料匯出（C-4）、雲端部署與入口層限流（C-5）、訂單（C-6）、RPG；也不含 Google-only 帳號的「重新驗證後設定密碼」設定頁、變更 email、多因素驗證、條款版本更新後的重新同意、CSP 等安全標頭。

## Capabilities

### New Capabilities

- `member-registration`: 使用者資料表與欄位慣例、信箱加密碼註冊、條款同意紀錄、email 正規化與 gmail 別名去重、通用回應、`EMAIL_VERIFICATION_REQUIRED` 兩種模式、服務條款與隱私權政策頁面。
- `session-authentication`: 密碼雜湊與長度規則、登入與通用失敗訊息、登入失敗鎖定、簽章 session cookie 與 `token_version` 撤銷、登出、登出所有裝置、改密碼、目前使用者查詢、帳號狀態對登入的影響、認證回應不可快取。
- `email-token-flows`: `auth_tokens` 的發放、雜湊、有效期、單次使用與取代舊權杖；信箱驗證、重寄（含頻率限制）、忘記密碼與重設密碼；確認頁（GET）與完成動作（POST）分離；SMTP 寄信與信件內容；提交後才寄送。
- `google-sign-in`: OIDC 授權碼流程與 PKCE、`state`、`nonce`、ID token 驗證、`user_identities`、同信箱帳號合併規則、首次 Google 註冊的條款同意、Google 設定為選用。
- `csrf-protection`: 所有非 GET 請求的 CSRF 權杖與 `Origin` 驗證、權杖的發放與輪替、前端預設帶標頭。
- `access-control`: `AccessPolicy`（擁有者與管理員判斷）、非擁有者一律 404、角色與會員等級的語意界線、IDOR 測試框架、首位管理員的建立指令。
- `auth-pages`: 登入、註冊、忘記／重設密碼、信箱驗證、帳號設定頁的行為（Vue 全域變數模式、通用訊息、`finally` 還原 UI、`next` 轉址安全、純文字渲染、無障礙）。

### Modified Capabilities

- `app-configuration`: 新增 `EMAIL_VERIFICATION_REQUIRED`、`BASE_URL`、寄信、Google OAuth 與 `SECRET_KEY_FALLBACKS` 設定及其在各環境的驗證；`SECRET_KEY` 在 staging 與 production 須達最小長度。
- `application-layering`: 層級依賴規則納入 HTTP 安全輔助層（可用 Flask、不可碰資料庫）；Service 的業務錯誤統一對應 HTTP 狀態與繁體中文訊息；認證相關回應禁止快取。
- `database-access`: 請求級交易新增「提交成功後才執行」的延後動作（供寄信使用），回復或錯誤回應時一律丟棄；以及 4xx 回應後仍可保留「失敗記帳」（登入失敗次數）的明確例外。
- `service-observability`: 日誌允許欄位新增安全事件名稱，並規定認證事件的記錄內容與禁止內容。

## Impact

- **新增程式碼**：`app/models/user.py`、`app/repositories/`（使用者、權杖）、`app/services/`（認證、信件權杖、Google、`AccessPolicy`、寄信、錯誤類型）、`app/security/`（CSRF、session cookie、安全轉址）、`app/runtime.py`、`app/cli.py`、兩個 Controller 檔、`app/templates/auth/` 與 `legal/`、`app/static/js/auth/`、`app/static/css/auth.css`、migration `0002`、對應測試。
- **修改程式碼**：`app/config.py`（新設定與驗證）、`app/__init__.py`（註冊新元件與命令；`create_app` 增加可注入寄信與身分供應商的選用參數供測試使用）、`app/db/session.py`（提交後動作）、`app/observability/json_logging.py`（新欄位）、`app/controllers/errors.py`、`app/templates/base.html`（CSRF meta 與共用腳本）、層級檢查測試、`.env.example`、`README.md`。
- **新增相依套件**（Python 3.14，實作時再確認各套件有預編譯版本）：`argon2-cffi`（Argon2id）、`PyJWT[crypto]`（ID token 簽章驗證）；開發用 `aiosmtpd`（本機 SMTP 擷取與寄信整合測試）。不使用 Flask-WTF、Authlib、Flask-Limiter（理由見 design）。
- **資料庫**：新增 migration（只新增三張資料表，符合 expand 原則，不改動既有結構）；開發用 `project` 與測試用 `project_test` 皆需升級。
- **環境前提**：本機測試不需要真實的 Google 用戶端或 SMTP 服務（以模擬供應商與本機 SMTP 取代）。使用者**選擇性**準備：開發用 Google OAuth 用戶端（人工驗收真實 Google 流程時才需要，不阻擋實作）；正式環境的寄信服務與網域屬上線前關卡（C-5）。
- **不修改**：`AGENTS.md`（本 change 的做法均在其範圍內，未發現需要新增條文）、既有 skill。
- **後續 change 的相依**：C-3 以 `login_required`、`AccessPolicy` 與 IDOR 測試框架建構 `/api/cs/*`；C-4 在此基礎上加入配額、管理員後台、停權與刪除帳號。
