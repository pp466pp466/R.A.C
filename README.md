# 開放世界文字 RPG × 會員制智能客服

以 LLM 驅動的雙核心平台。開發規範以根目錄的 [AGENTS.md](AGENTS.md) 為唯一權威來源；設計決策見 [docs/report.md](docs/report.md)，變更提案見 `openspec/changes/`。

目前進度：C-1 `platform-foundation`（專案地基）、C-2 `member-auth`（會員驗證）與 `age-gate`（年齡確認閘門）都已封存；後續為 C-3 客服對話（見 `docs/report.md` 第 5 章）。**已知落差**：AGENTS.md 第 10 節已改為「生日加年齡區間」的年齡確認，程式仍是 `age-gate` 的勾選自行聲明，由後續的 `age-verification-birthdate` 實作（見 `docs/RPG.md` 的 T-10）。

## 環境需求

| 項目 | 需求 |
|---|---|
| 作業系統 | Windows 10（開發）；Linux 容器（部署） |
| Python | 3.14（見 `.python-version`） |
| 資料庫（開發） | 本機 Microsoft SQL Server，並安裝 ODBC Driver 18 |
| 版本控制 | Git |
| 容器（可選） | WSL 2 內的 Docker Engine（見下方） |
| Node.js（可選） | 僅用於前端打字機緩衝區的單元測試（`node --test`） |

## 安裝

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## 執行與測試

```powershell
# 本機開發伺服器（Windows 無法執行 gunicorn；正式環境與容器才使用 gunicorn）
.\.venv\Scripts\python.exe -m flask --app wsgi run

# 測試（預設使用記憶體 SQLite，不需要任何機密）
.\.venv\Scripts\python.exe -m pytest

# 只跑 SQLite（不連 SQL Server）／只跑真實 SQL Server 的整合測試（需要 TEST_SQLSERVER_URL，見下方「資料庫與 migration」）
.\.venv\Scripts\python.exe -m pytest -m "not sqlserver"
.\.venv\Scripts\python.exe -m pytest -m sqlserver

# 前端純函式的單元測試
node --test "tests/js/**/*.test.js"
```

## 環境變數與機密

設定以環境變數 `APP_ENV` 選擇，**沒有預設值**：未設定或不合法會拒絕啟動。可用的值為 `development`、`testing`、`staging`、`production`。

| 變數 | 必填 | 說明 |
|---|---|---|
| `APP_ENV` | 是 | 執行環境，必須由行程環境變數提供 |
| `SECRET_KEY` | staging、production | 工作階段簽章金鑰；development 缺漏時使用臨時金鑰 |
| `DATABASE_URL` | development、staging、production | 資料庫連線字串；testing 預設為記憶體 SQLite |
| `LOG_LEVEL` | 否 | 日誌層級，預設 `INFO` |
| `FLASK_DEBUG` | 否 | 本機除錯模式；staging 與 production 設定即啟動失敗 |
| `TEST_SQLSERVER_URL` | 否 | SQL Server 整合測試用連線字串，必須指向 `project_test` |
| `SECRET_KEY_FALLBACKS` | 否 | 簽章金鑰輪替期間仍可驗證的舊金鑰（以逗號分隔）；新簽發的內容一律用 `SECRET_KEY` |
| `EMAIL_VERIFICATION_REQUIRED` | 否 | `true` 或 `false`。未設定時 development、testing 為 `false`，staging、production 為 `true`；production 設為 `false` 會啟動失敗 |
| `BASE_URL` | 信箱驗證或 Google 登入啟用時 | 對外網址（不含路徑），用於信件連結與 Google 回呼；staging 與 production 須為 `https` |
| `MAIL_SMTP_HOST` | 信箱驗證開啟時 | 寄信（SMTP）主機 |
| `MAIL_SMTP_PORT` | 信箱驗證開啟時 | 寄信連接埠（1 至 65535） |
| `MAIL_SMTP_SECURITY` | 否 | `starttls`（預設）、`ssl`、`none`；`none` 只允許 development 與 testing |
| `MAIL_SMTP_USER` | 否 | 寄信帳號；須與 `MAIL_SMTP_PASSWORD` 同時設定或同時省略 |
| `MAIL_SMTP_PASSWORD` | 否 | 寄信密碼（機密） |
| `MAIL_FROM` | 信箱驗證開啟時 | 寄件者地址（單純的電子郵件地址） |
| `GOOGLE_OAUTH_CLIENT_ID` | 否 | Google 登入的用戶端識別碼；與密鑰須同時設定才會啟用 |
| `GOOGLE_OAUTH_CLIENT_SECRET` | 否 | Google 登入的用戶端密鑰（機密） |

規則：

- 只有 `development` 會讀取專案根目錄的 `.env`，且行程環境變數優先；`staging` 與 `production` 只使用行程環境變數（由平台機密功能注入）。
- 複製 `.env.example` 為 `.env` 後填入實際值。`.env` 已被 `.gitignore` 排除，**絕不可提交**；機密也不得寫入程式碼、文件或日誌。
- `staging` 與 `production` 缺少必要機密、資料庫連線使用 `sa` 帳號、連線字串含 `TrustServerCertificate=yes`、或開啟除錯模式，都會啟動失敗。後兩項只允許在本機開發使用。
- 設定物件與連線字串在任何輸出中都會遮罩密碼。
- `SECRET_KEY` 在 `staging` 與 `production` 至少 32 個字元。
- **信箱驗證**：本機與預備環境可以關閉（註冊後直接啟用、不寄信、忘記密碼一併停用）；`production` 必須開啟並完成寄信設定與 `BASE_URL`。關閉期間註冊的帳號信箱驗證時間為空，之後以 Google 登入同信箱時，依合併規則會清除其未驗證的密碼憑證。
- **Google 登入為選用**：識別碼與密鑰兩者都設定才啟用，只設定一個會啟動失敗；啟用時必須設定 `BASE_URL`。在 Google Cloud Console 把「已授權的重新導向 URI」設為 `<BASE_URL>/auth/google/callback`，本機瀏覽器請一律使用 `BASE_URL` 的主機名稱（例如 `http://localhost:5000`），不要改用 `127.0.0.1`。
- Gemini 金鑰（`GEMINI_API_KEY`）由客服對話 change（C-3）接上，目前只需存在於本機 `.env`。

## 建立首位管理員

網頁註冊與任何 API 都無法建立管理員或提升角色；首位管理員由部署者在已設定好環境變數與資料庫的環境中，以一次性指令建立：

```powershell
# 互動式：依序詢問電子郵件、顯示名稱，密碼以不回顯的方式輸入兩次
.\.venv\Scripts\python.exe -m flask --app wsgi create-admin

# 非互動式（例如容器的一次性工作）：信箱與名稱用參數，密碼從標準輸入讀一行
"<密碼>" | .\.venv\Scripts\python.exe -m flask --app wsgi create-admin --email ops@example.com --display-name 維運 --password-stdin
```

- 密碼**不接受命令列參數**（會留在 shell 歷史與行程清單）；密碼與電子郵件都不會出現在輸出或日誌。
- Windows PowerShell 5.1 以管線傳字串給外部程式時，會在最前面加上 UTF-8 BOM；指令會自動去除它，所以上面的管線寫法可以放心使用（否則密碼會被存成「BOM＋密碼」，之後無法登入）。
- 建立的帳號角色為 `admin`、狀態為 `active`、信箱視為已驗證；密碼規則與一般註冊相同（8 至 128 字）。
- 信箱（含 Gmail 別名的重複判斷）已有帳號時指令會失敗，且不會改動既有帳號。
- **年齡確認不會代為完成**：部署者建立帳號不等於本人確認年滿 18 歲，所以管理員帳號的 `age_confirmed_at` 為空。管理員**第一次登入後須到 `/account` 自行勾選確認**，否則和其他會員一樣不能使用任何會呼叫 Gemini 的功能（見「撰寫新端點」）。

## 會員驗證（C-2）

註冊、登入、信箱驗證、重設密碼、Google 登入與帳號設定。規格見 `openspec/specs/`（`member-registration`、`session-authentication`、`email-token-flows`、`google-sign-in`、`csrf-protection`、`access-control`、`auth-pages`）。

### 端點

| 端點 | 方法 | 用途 | 需要登入 |
|---|---|---|---|
| `/api/auth/register` | POST | 註冊（不論信箱是否已存在，回應完全相同；不自動登入）。請求欄位 `email`、`password`、`display_name`、`accept_terms`、`confirm_adult`，後兩者各自獨立、都必須明確為 `true` | 否 |
| `/api/auth/login` | POST | 登入（失敗一律是同一則通用訊息） | 否 |
| `/api/auth/logout` | POST | 登出目前裝置 | 否 |
| `/api/auth/logout-all` | POST | 登出所有裝置（含目前這一台） | 是 |
| `/api/auth/me` | GET | 目前登入者本人的資料：`public_id`、`email`、`display_name`、`member_tier`、`role`、`status`、`email_verified`、`has_password`、`age_confirmed`（布林，確認時間與政策版本不對外）、`google_linked` | 是 |
| `/api/auth/confirm-adult` | POST | 補確認年滿 18 歲（請求 `{"confirm_adult": true}`，回應最新的帳號資料；重複確認成功但不改動第一次的紀錄） | 是 |
| `/api/auth/change-password` | POST | 變更密碼（其他裝置被登出，目前裝置維持登入） | 是 |
| `/api/auth/verify-email` | POST | 以權杖完成信箱驗證（驗證關閉時 404） | 否 |
| `/api/auth/resend-verification` | POST | 重寄驗證信（驗證關閉時 404） | 是 |
| `/api/auth/forgot-password` | POST | 忘記密碼（回應與信箱是否存在無關；驗證關閉時 404） | 否 |
| `/api/auth/reset-password` | POST | 以權杖設定新密碼（驗證關閉時 404） | 否 |
| `/api/auth/google/start` | POST | 發起 Google 登入（Google 未設定時 404）。請求欄位 `accept_terms`、`confirm_adult`、`next`；第一次以 Google 註冊時兩項確認都需要，既有帳號登入不需要 | 否 |

| 頁面 | 說明 |
|---|---|
| `/login`、`/register` | 登入、註冊（已登入者導向 `/account`） |
| `/forgot-password`、`/reset-password`、`/verify-email` | 信箱驗證開啟時才存在；權杖在網址片段，頁面載入時不會自動完成動作 |
| `/account` | 帳號設定（需登入）：顯示會員等級標籤與年齡確認狀態（尚未確認時可在此補確認）、變更密碼、登出、登出所有裝置 |
| `/terms`、`/privacy` | 服務條款與隱私權政策（**草稿**，待法務覆核） |
| `/auth/google/callback` | Google 登入的回呼（回傳重新導向，不是 JSON） |

所有非 GET 請求都必須帶 `X-CSRF-Token`（取自頁面 `<meta name="csrf-token">`，axios 預設已帶）並通過 `Origin` 檢查；沒有任何端點可以豁免。

### 設計重點與已知限制

- **登入狀態**：簽章 cookie，內容只有使用者編號與 `token_version`，存活 7 天（自登入起算，不隨使用延長）；改密碼、登出所有裝置、重設密碼、綁定 Google 時 `token_version` 加 1，既有登入狀態立即失效。
- **不洩漏帳號是否存在**：註冊、忘記密碼、登入失敗的回應完全相同；信箱不存在與密碼錯誤的運算量相當；寄信在回應送出之後才進行。
- **登入失敗**：同一帳號連續 5 次失敗鎖定 15 分鐘，計數存放於資料庫。請求交易在 4xx 時預設回復，所以失敗的記帳會明確要求保留（見 `keep_changes_on_error`）。
- **預先佔用信箱**：待驗證帳號被重複註冊時不覆蓋原密碼；受害者可用「忘記密碼」取回（成功後攻擊者的登入狀態與密碼都失效）；Google 登入合併時會清除未驗證帳號的密碼並撤銷其登入狀態。**殘留風險**：受害者若點了攻擊者觸發的驗證信又不察覺，攻擊者仍可登入——這是「先註冊後驗證」模式的固有限制。
- **Google-only 帳號**：目前沒有「在帳號頁設定密碼」的功能（延後）；信箱驗證開啟時，可用「忘記密碼」流程設定密碼。
- **預設登入後頁面**是 `/account`（C-3 完成後改為 `/cs`，在 `app/security/redirects.py` 一個常數）。
- **登入鎖定可被用於阻斷特定帳號**（每 15 分鐘一次）：這是規範指定的行為，緩解靠入口層的 IP 限流（C-5）。
- **開發資料庫不可啟用 SQL Server 的帳本（Ledger）**：帳本會把資料列的更新與刪除永久保留在歷史表，讓「刪除帳號時真的刪除個資」（C-4）無法成立，也會讓失敗次數這類頻繁更新的欄位累積大量歷史列，而且建立後無法關閉。本機原本的 `project` 就是啟用帳本建立的，已在 2026-10-28 重建：舊的改名保留為 `project_old_ledger`（沒有任何資料，確認不需要後可自行刪除），新的 `project` 以 `Chinese_Taiwan_Stroke_CI_AS` 定序、不啟用帳本，並已升級到最新 migration。新建資料庫時請**不要**勾選「啟用帳本」（`CREATE DATABASE` 預設不啟用）。Cloud SQL 是否支援或預設啟用帳本，C-5 要確認。

### 瀏覽器驗收紀錄（2026-10-28）

以 Edge 無頭模式（CDP）對暫時的伺服器（SQLite、信箱驗證開啟、本機 SMTP 擷取、`.env` 內的真實 Google 用戶端設定）逐項檢查，**84 / 84 項通過**。

| 範圍 | 檢查內容 |
|---|---|
| 版面與資源（5 頁 × 1280px 與 360px，共 40 項） | 回應 200 與繁體中文標題；沒有水平捲軸；沒有外部請求、失敗請求與主控台錯誤；語言、深色主題、配色（`#0F1012`、`#E2E4E9`）與 Noto Sans TC Variable 字型 |
| 註冊 | 全部留空顯示各欄位的繁體中文錯誤；失敗後按鈕恢復；密碼太短的欄位錯誤與 `aria-invalid`、`role=alert`；成功後只顯示通用訊息；擷取到驗證信 |
| 驗證連結 | 開啟後網址列的權杖片段被移除；載入時沒有送出任何 API 請求；按下確認才完成；同一權杖再用被拒絕；沒有權杖時顯示說明 |
| 登入 | 密碼錯誤與信箱不存在得到相同訊息、按鈕恢復、密碼欄位清空；連續失敗鎖定後即使密碼正確也被拒絕且訊息不變；Tab 順序與 Enter 送出 |
| 帳號頁與 Modal | 顯示名稱、信箱、等級標籤「一般」；Vue 掛載前不顯示 `{{ }}`；360px 無水平捲軸；重寄驗證信的頻率限制訊息；變更密碼 Modal（Esc 關閉、目前密碼錯誤時保持開啟、Enter 送出、成功後 Toast 且仍為登入狀態）；登出所有裝置先以 Modal 確認、確認後導向登入；登出後 `next` 保留並可用新密碼登入 |
| 忘記與重設密碼 | 通用訊息；重設連結的權杖片段被移除、載入時不送出請求；兩次輸入不一致在前端擋下；成功後顯示完成訊息 |
| Google 入口 | 未勾選同意不離開本站；勾選後先呼叫 `/api/auth/google/start`，再導向 Google 授權網址（含 PKCE S256、`state`、`nonce`、`openid email profile` 與 `http://localhost:5000/auth/google/callback`） |
| 回呼錯誤與 404 | `?error=` 只顯示固定的繁體中文提示，外部文字不會出現在頁面；找不到的頁面為繁體中文錯誤頁 |

驗收中發現並已修正的問題：Vue 掛載前會短暫顯示原始 `{{ }}` 範本（加上 `v-cloak`）；變更密碼 Modal 裡按 Enter 不會送出（補上隱藏的送出按鈕）。

### 年齡確認的瀏覽器驗收紀錄（age-gate，2026-10-28）

同樣以 Edge 無頭模式（CDP）對暫時的伺服器（SQLite 檔案資料庫、信箱驗證關閉、假的 Google 供應商，資料庫中預先放了兩個沒有年齡確認紀錄的帳號）逐項檢查，**50 / 50 項通過**。假的 Google 供應商只驗證「是否呼叫 `/api/auth/google/start` 並導向授權網址」，真實的 Google 登入仍需手動確認（見下方）。

| 範圍 | 檢查內容 |
|---|---|
| 註冊頁的兩個勾選 | 同意條款與年滿 18 歲是兩個獨立的勾選、預設都未勾選，年齡標籤不在條款那一行；只缺年齡、只缺條款、兩者都缺，各自在**自己的勾選框下方**出現對應的錯誤（`role=alert`、`aria-invalid` 只標在缺少的那一項）；失敗後按鈕恢復、沒有建立帳號；兩個勾選都能用 Space 切換、Enter 送出；成功只顯示通用訊息，且請求次數正確 |
| Google 入口（註冊頁與登入頁） | 兩項都沒勾、只勾條款、只勾年齡：都不離開本站、不呼叫 API，並只提示缺少的那一項；兩項都勾才呼叫 `/api/auth/google/start` 並導向授權網址；`/login?error=age_required` 顯示固定提示，未知代碼不顯示任何外部文字 |
| 帳號設定頁補確認 | 尚未確認的帳號顯示「年齡確認＝尚未確認」與說明區塊；沒勾選就按「確認」不送出請求並顯示錯誤；勾選框可用鍵盤切換；成功後只送出一次 `POST /api/auth/confirm-adult`、清單改為「已確認」、說明區塊消失、Toast 回報；重新整理後維持已確認；`/api/auth/me` 的 `age_confirmed` 為 `true` 且不含確認時間與政策版本；新註冊的帳號登入後直接是已確認 |
| 條款與隱私權政策 | 條款載明僅限年滿 18 歲、另外勾選確認、不蒐集生日年齡證件；隱私權政策列出年齡確認紀錄與保存期限；兩頁都仍標示草稿 |
| 版面（5 頁 × 1280px 與 360px，另加 360px 的未確認帳號頁與註冊頁） | 回應 200 且停留在該頁（已登出才檢查登入、註冊頁，避免被導向 `/account`）；沒有水平捲軸、外部請求、失敗請求與主控台錯誤；360px 下勾選框與確認按鈕都在畫面寬度內 |

驗收中發現並已修正的問題：登入頁 Google 入口的條款錯誤訊息緊貼在下一個（年齡）勾選框上，看起來像是屬於年齡勾選。這是斷言檢查不出來、看截圖才發現的版面問題，已改為錯誤訊息貼在自己的勾選框下方並與下一個元素保持距離。

### 其他驗收紀錄（2026-10-28）

| 項目 | 結果 |
|---|---|
| 自動化測試 | `pytest` 1277 項通過（SQLite 1067 項、真實 SQL Server 210 項）；`node --test` 47 項；`openspec validate member-auth --strict` 通過 |
| 真實 SQL Server 的端到端走查 | 對 **`project_test`** 以 `flask run` 走完註冊、大小寫不同的重複註冊、登入、繁體中文與表情符號的顯示名稱往返、變更密碼（其他裝置登出）、登出所有裝置、連續 5 次失敗後鎖定（鎖定狀態在 401 之後仍累積）、`flask create-admin` 建立的管理員登入，**17 / 17 項通過**，日誌沒有信箱與密碼；結束後以 `alembic downgrade base` 還原。當時開發用的 `project` 還啟用著帳本功能（見上方「已知限制」，現已重建），測試帳號的資料列會永久留在歷史表，所以改用 `project_test` |
| Google OAuth 用戶端憑證 | 以 `.env` 內的用戶 ID 與密鑰向 Google 的 token 端點送一個故意錯誤的授權碼，回應 `400 invalid_grant: Malformed auth code`（而不是 `401 invalid_client`），代表這組憑證有效。此檢查**不能**證明重新導向 URI 已登錄、測試使用者已加入，那兩項要走一次真實的 Google 登入才會知道 |
| 容器 | 56 / 56 項通過（見「容器建置與驗收」） |
| 機密掃描 | 對會被提交的 354 個檔案與全部 git 歷史搜尋 `.env` 內的真實機密值與常見金鑰格式：0 筆命中；`.env` 被忽略且未追蹤；`.env.example` 沒有任何實際值 |

驗證 `create-admin` 時發現並已修正：Windows PowerShell 5.1 以管線傳密碼會夾帶 UTF-8 BOM，導致密碼被存成「BOM＋密碼」而無法登入（見「建立首位管理員」）。

**仍需你目視或手動確認**：真實的 Google 登入完整流程（需要在 Google Cloud Console 登錄重新導向 URI 並把你的帳號加入測試使用者）、真實信箱的驗證信到達與顯示。

### 年齡確認的其他驗收紀錄（age-gate，2026-10-28）

| 項目 | 結果 |
|---|---|
| 自動化測試 | `pytest` 1571 項通過（SQLite 1317 項、真實 SQL Server `project_test` 254 項）；`node --test` 47 項；`openspec validate age-gate --strict` 通過。其中包含 AGENTS.md 手動修訂後的同步（密碼長度 8 至 128 字，以及第 10 節與 skill 鏡像的整條逐字比對） |
| 年齡閘門的 HTTP 行為 | 以只在測試應用程式註冊的端點（`tests/support/age_gate.py`）驗證：未確認 → 403 `age_confirmation_required`、補確認後同一端點 200；管理員與 `vip` 都不豁免；信箱未驗證時回 `forbidden` 而不是引導補確認年齡；被拒時日誌只有 `auth.age_gate_denied` 與內部使用者編號 |
| 並行確認 | 8 個連線同時對同一帳號補確認，恰好 1 個寫入（SQLite 與 SQL Server 都通過），第一次寫入的時間與版本不會被覆蓋 |
| 容器 | 67 / 67 項通過，映像 271 MB（見「容器建置與驗收」） |
| 本機開發資料庫 `project` | 先以 `BACKUP DATABASE … COPY_ONLY, CHECKSUM` 備份（`RESTORE VERIFYONLY` 通過），再 `alembic upgrade head`：`0002` → `0003_users_age_confirmation`。升級後 `alembic current` 為 `0003 (head)`、`/healthz` 與 `/readyz` 回 200、兩個新欄位存在且可為空；當時 `users`、`user_identities`、`auth_tokens` 都是 0 筆，沒有資料需要回填 |
| migration `0003` | 升級只新增兩個可為空的欄位；降級只刪這兩欄、不影響其他資料表的資料列（SQLite 預設以重建資料表刪欄位，會連帶清掉以串聯刪除參照 `users` 的資料，已改為直接 `DROP COLUMN` 並由測試守住） |

## 本機測試信箱驗證

本機預設**關閉**信箱驗證（註冊後直接啟用、不寄信、忘記密碼停用）。要在本機實際走一遍「註冊 → 收驗證信 → 點連結」，不需要任何真實的寄信服務：用 `scripts/dev_smtp_sink.py` 在本機收信，信件內容會印在終端機。

```powershell
# 終端機 1：啟動擷取伺服器（信件內容與連結會印在這裡；加 --save 資料夾可另存 .eml）
.\.venv\Scripts\python.exe scripts\dev_smtp_sink.py

# 終端機 2：開啟信箱驗證並讓應用程式寄到它（行程環境變數優先於 .env，所以不必修改 .env）
$env:EMAIL_VERIFICATION_REQUIRED = "true"
$env:MAIL_SMTP_HOST = "127.0.0.1"; $env:MAIL_SMTP_PORT = "1025"
$env:MAIL_SMTP_SECURITY = "none"; $env:MAIL_FROM = "service@example.com"
.\.venv\Scripts\python.exe -m flask --app wsgi run
```

- 需要 `BASE_URL`（`.env` 已設為 `http://localhost:5000`）；信件中的連結一律由它組成，不使用請求的主機標頭。
- 連結形如 `<BASE_URL>/verify-email#token=…`：權杖放在網址片段，伺服器與入口層的日誌都看不到它。把終端機印出的內容當作機密。
- 寄信在「交易提交成功、回應送出之後」才進行；寄信失敗只會在日誌留下 `mail.send_failed` 事件，不影響註冊結果，使用者可以在帳號頁重寄。
- 只開 `ssl` 或 `starttls` 才能連真實的寄信服務；`none`（不加密）只允許 development 與 testing。
- 若擷取伺服器啟動時出現 `WinError 10013`（埠被 Windows 保留），用 `--port` 換一個埠，並同步修改 `MAIL_SMTP_PORT`。

**驗收紀錄（2026-10-28）**：依本節操作，以暫存的 SQLite 資料庫與埠 5055，用腳本從註冊走到驗證連結，結果如下。

| 步驟 | 結果 |
|---|---|
| 註冊（驗證開啟） | 200；回應是通用訊息；帳號為 `pending_verification` |
| 擷取伺服器收到的信 | 1 封，UTF-8 繁體中文，標題與內文正確，收件人為註冊的信箱 |
| 信中的連結 | `<BASE_URL>/verify-email#token=…`（權杖在片段，不在查詢字串） |
| 驗證前登入 | 200，狀態 `pending_verification`、`email_verified=false` |
| 以連結內的權杖驗證 | 200；原本的登入狀態立即失效（401） |
| 重新登入 | 200，狀態 `active`、`email_verified=true` |
| 同一權杖再用一次 | 400 `invalid_token` |
| 日誌事件順序 | `auth.register` → `auth.verification_sent` → `auth.login_success` → `auth.email_verified` → `auth.login_success` → `auth.email_token_rejected`（皆不含信箱與權杖） |

此外確認：擷取伺服器沒有啟動時，註冊回應仍照常在約 0.1 秒內送出（寄信在回應之後進行），約 5 秒（寄信逾時）後日誌出現 `mail.send_failed`，不含收件人地址。

## 撰寫新端點：權限與 IDOR 測試

屬於使用者的資源（對話、訊息……）的每個讀寫端點，都要遵守下列規則，並用 `tests/support/idor.py` 的框架測試：

- 端點取得登入者用 `require_login()`（`app/security/session.py`）；**不要**自己寫權限判斷，一律問 `AccessPolicy`（`get_access_policy()`）。
- 非擁有者與資源不存在必須得到**完全相同**的 404：用 `AccessPolicy.require_owner()`（它拋出 `NotFound`），不要回 403。
- Repository 方法簽名必須帶 `user_id`，查詢一律帶 `user_id` 條件；不提供只憑 ID 取資料的方法給 API 路徑使用。
- 會員等級（`member_tier`）只影響額度，不可用在權限判斷。
- **每個會呼叫 Gemini 的端點（客服、RPG……）必須先呼叫 `get_access_policy().require_chat_access(user)`**，再做任何事：它依序檢查帳號狀態、信箱驗證與「已確認年滿 18 歲」，年齡不足時回 403 與代碼 `age_confirmation_required`（前端據此引導到 `/account` 補確認）。管理員與各會員等級都不豁免；端點不要自己判斷年齡。寫法見 `tests/support/age_gate.py`，HTTP 行為由 `tests/test_age_gate_api.py` 驗證。

```python
from tests.support.idor import EndpointSpec, check_endpoint

spec = EndpointSpec("DELETE", "/api/cs/conversations/{id}", create_resource=make_conversation, success_status=200)
check_endpoint(app, spec, owner=alice, intruder=bob)
```

`check_endpoint` 依序以「他人 → 不存在的識別碼 → 未登入 → 擁有者」呼叫端點：他人必須 404、不存在的識別碼必須 404 且**回應與「他人」逐字相同**（否則等於告訴對方資源存在）、未登入必須 401、擁有者成功。擁有者放在最後，因為它可能刪除資源；若先執行，對他人的檢查就是對已不存在的資源做的，什麼也證明不了。失敗時拋出 `IdorFailure`，`identity` 指出是哪一種身分造成的。框架本身有自我驗證測試（`tests/test_idor_harness.py`），證明它抓得出故意寫錯的端點。

## 資料庫與 migration

系統同時支援 **SQLite**（快速單元測試）與 **SQL Server**（本機開發、整合測試、正式環境），兩者共用同一套 ORM 程式碼。

| 項目 | 規則 |
|---|---|
| 原生 SQL | 全面禁止，唯一例外是 `app/db/dialect.py` 的 SQLite 連線初始化；由 `tests/architecture/` 的靜態掃描強制 |
| 連線初始化 | 只有 SQLite 會自動套用預寫日誌、外鍵強制檢查與忙碌等待；SQL Server 不執行任何 SQLite 設定 |
| 交易 | 每個請求一個 Session：成功（狀態碼小於 400）時提交，否則回復，一定釋放連線 |
| 唯一字串 | 先經 `normalize_unique_text()` 正規化再寫入與查詢（SQLite 區分大小寫、SQL Server 不區分） |
| 可為空的唯一欄位 | 使用 `unique_when_not_null()`；直接 `unique=True` 會讓 SQL Server 只允許一個空值 |
| 資料表結構 | 只能透過 Alembic migration 變更；應用程式啟動時不會建立資料表，也不會自動執行 migration |

### 指令

```powershell
# 升級到最新版本（開發者在 .env 寫了 APP_ENV=development，工具會自動採用；應用程式本身不會）
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current

# 新增 migration（只使用 Alembic op API 與 ORM 模型，不得含原生 SQL）
.\.venv\Scripts\python.exe -m alembic revision -m "說明"

# 測試：預設只跑 SQLite；加上 -m sqlserver 才跑 SQL Server 整合測試
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest -m sqlserver
```

### SQL Server 整合測試的前置條件

1. 本機 SQL Server 已啟動，並安裝 ODBC Driver 18。
2. 已建立**獨立的** `project_test` 資料庫（測試會建立並刪除自己的資料表，**絕不可指向開發用的 `project`**）。
3. 在 `.env` 設定 `TEST_SQLSERVER_URL` 指向 `project_test`。資料庫名稱不是以 `_test` 結尾時，測試會直接失敗而不是略過。

### 注意事項

- `alembic.ini` 必須是**純 ASCII**：Alembic 用系統地區編碼讀取它（繁體中文 Windows 為 cp950），中文註解會造成解析失敗。
- `alembic.ini` 刻意不含連線字串，連線一律取自 `DATABASE_URL`。
- 基線 revision 只建立 Alembic 版本機制，不建立業務資料表。

## 日誌欄位與健康檢查

日誌是結構化 JSON，每行一筆，輸出到標準輸出。採**白名單**：沒有列在下表的欄位一律丟棄。

| 欄位 | 說明 |
|---|---|
| `time` | UTC 時間（ISO 8601，毫秒） |
| `level` | 日誌層級 |
| `logger` | 記錄器名稱 |
| `message` | 訊息文字（會再遮罩密碼、權杖、連線字串中的帳密） |
| `request_id` | 請求識別碼 |
| `path` | 路由規則（例如 `/verify/<token>`）；未比對到路由時為路徑本身。**永遠不含查詢字串** |
| `method` | HTTP 方法 |
| `status` | 狀態碼 |
| `latency_ms` | 處理延遲（毫秒） |
| `user_id` | 內部使用者編號（整數，已登入時才有） |
| `conversation_id` | 內部對話編號（整數） |
| `intent` | 客服意圖（列舉值） |
| `model` | 模型名稱 |
| `attempt` | 嘗試次數 |
| `prompt_tokens` | 輸入 token 數 |
| `completion_tokens` | 輸出 token 數 |
| `finish_reason` | 模型結束原因 |
| `error_type` | 例外類型 |
| `error_location` | 例外發生的檔名與行號 |
| `event` | 認證與安全事件名稱（固定列舉，例如 `auth.login_failed.bad_password`；失敗原因寫在名稱最後一段，因此不會夾帶自由文字） |

**絕不記錄**：密碼與密碼雜湊、工作階段 Cookie、CSRF 與驗證／重設權杖、授權標頭、完整 API 金鑰、使用者 email、使用者訊息全文、模型回覆與原始輸出，以及含權杖的完整網址。例外只記錄類型與位置，**不記錄例外訊息與堆疊**（訊息可能夾帶機密）。

### 請求識別碼

每個請求都有識別碼，回傳於回應標頭 `X-Request-ID`，並出現在該請求的所有日誌。上游提供的值必須符合 `^[A-Za-z0-9_-]{8,64}$` 才會沿用，否則重新產生。

### 健康檢查

| 端點 | 用途 | 成功 | 失敗 |
|---|---|---|---|
| `/healthz` | 存活：只表示行程還在，**不碰資料庫** | 200 | — |
| `/readyz` | 就緒：實際確認資料庫可用 | 200 | 503 |

兩者都不需登入，回應只有 `{"status": "ok"}` 或 `{"status": "unavailable"}`，不含版本、設定、連線字串或錯誤細節，並帶有 `Cache-Control: no-store`。

## 離線資產與更新方式

前端完全離線：所有第三方函式庫與字型都存放在本機，頁面載入時**不會連線任何外部網站**。禁止 CDN、Google Fonts、`.vue`、Vite、外部彈窗套件與 Bootstrap 的 JavaScript，由 `tests/architecture/test_frontend_rules.py` 強制。

### 目前的資產

| 資產 | 版本 | 授權 | 位置 |
|---|---|---|---|
| Vue（全域生產版） | 3.5.44 | MIT | `app/static/vendor/vue/` |
| Bootstrap（只用 CSS） | 5.3.8 | MIT | `app/static/vendor/bootstrap/` |
| Axios | 1.20.0 | MIT | `app/static/vendor/axios/` |
| Noto Sans TC（可變字型，字重 100 至 900） | 5.3.1 | OFL-1.1 | `app/static/fonts/` |

字型共 108 個依字元範圍（`unicode-range`）切片的 woff2，總量約 **4.1 MB**；瀏覽器只會下載頁面實際用到的切片。字型堆疊為 `Noto Sans TC Variable`，後備為 Microsoft JhengHei、PingFang TC。

每個資產的版本、來源、授權與 SHA-256 都記錄在 `app/static/MANIFEST.json`，並由測試重算比對；清單以外的第三方檔案不得出現在 `vendor/` 與 `fonts/`。

### 更新函式庫

以 Axios 為例（其他函式庫相同）。**下載的檔案一律視為不可信資料**：放在全新的空資料夾，不要在裡面執行任何東西。

```powershell
# 1. 查詢新版本
npm view axios dist-tags.latest

# 2. 在全新的空資料夾下載發行包，並比對 npm 登錄庫記載的完整性校驗碼（必須為 True）
$work = Join-Path $env:TEMP "asset-update"; New-Item -ItemType Directory $work | Out-Null; Set-Location $work
$tgz = (npm pack axios@<新版本> --silent | Select-Object -Last 1).Trim()
$expected = (npm view axios@<新版本> dist.integrity).Trim()
$actual = 'sha512-' + [Convert]::ToBase64String([Security.Cryptography.SHA512]::Create().ComputeHash([IO.File]::ReadAllBytes((Resolve-Path $tgz).Path)))
$actual -eq $expected
tar -xzf $tgz

# 3. 把需要的檔案（本體、.map、LICENSE）複製到 app/static/vendor/axios/
# 4. 編輯 app/static/MANIFEST.json：更新該資產的 version、source、upstream_integrity
# 5. 回到專案根目錄，重算校驗碼並檢查
.\.venv\Scripts\python.exe scripts\update_asset_manifest.py
.\.venv\Scripts\python.exe scripts\update_asset_manifest.py --check

# 6. 驗證，並在瀏覽器確認畫面正常、網路面板沒有外部請求
.\.venv\Scripts\python.exe -m pytest tests/test_assets_manifest.py tests/test_fonts_and_static.py tests/architecture/test_frontend_rules.py
```

字型的更新方式相同，來源套件為 `@fontsource-variable/noto-sans-tc`：複製 `files/` 內的 `*-wght-normal.woff2`，並把官方 `wght.css` 裡的 `url(./files/` 改成 `url(./` 後存成 `app/static/fonts/fonts.css`（開頭保留 `@charset "UTF-8";`），最後同樣重算清單。

## 共用元件

所有頁面的基礎樣板 `base.html` 都已載入共用的色彩（`theme.css`）、元件樣式（`components.css`）與三個腳本：`typewriter.js`、`toast-store.js`、`ui-components.js`。元件以 Vue 3 全域變數模式撰寫，**不使用 Bootstrap 的 JavaScript 與 `data-bs-*`**，所有文字一律以純文字渲染（不使用 `v-html`／`innerHTML`），內建文字為繁體中文。

```html
<script>
const { createApp, ref } = Vue;
const app = createApp({ setup() { const open = ref(false); return { open }; } });
UI.install(app);            // 註冊 <ui-modal> 與 <ui-toast-region>
app.mount('#app');
</script>
```

| 元件 | 用法 | 重點 |
|---|---|---|
| Modal | `<ui-modal v-model="open" title="..." status-code="SYSTEM_PROMPT">...</ui-modal>` | 由 `v-if` 接管；遮罩 `rgba(15, 16, 18, 0.85)` 加 `blur(8px)`；容器 `rgba(22, 24, 28, 0.95)` 加 1px `#2A2D34` 邊框；左上角小字狀態碼；Esc、點遮罩可關閉（`:closable="false"` 可停用）；焦點限制在對話框內，關閉後還原；開啟時背景不可捲動 |
| Toast | 頁面放一個 `<ui-toast-region>`，之後 `UI.toast('訊息', { type, duration })` | 右下角、左側 2px `#FF6B35` 線、由下往上淡入；預設 4 秒消失，`duration: 0` 不自動消失；多則依序堆疊；`type: 'error'` 使用 `role="alert"` |
| 打字機 | `new UI.TypewriterBuffer({ onUpdate, onDone })`；客服 `play(完整文字)`，RPG 串流 `push(片段)` … `finish()` | 以字素為單位，不切開中文、表情符號與組合字；積壓越多越快；`finish()` 後最晚 3 秒內顯示完；`flush()` 略過動畫；`cancel()` 取消 |

### 示範頁與測試

- **元件示範頁**：`/dev/components`，**只在 `development` 與 `testing` 環境註冊**；`staging` 與 `production` 沒有這個路徑，連開發用程式碼都不會被載入。
- **自動化測試**：`pytest` 會檢查樣式數值、結構與規則，並自動執行 Node 的前端單元測試（有安裝 Node 才執行）。也可單獨執行：`node --test "tests/js/**/*.test.js"`（Node 24 需要用萬用字元，不能傳資料夾）。

### 人工驗收紀錄（2026-10-27）

以 Edge 156 無頭模式，經 DevTools 協定對 `/dev/components` 做真實的點擊、按鍵與量測，共 **54 項全數通過**，並檢視截圖：

| 項目 | 結果 |
|---|---|
| 色彩與字型 | 主背景 `#0F1012`、文字 `#E2E4E9`；頁面上沒有任何純白文字；字型為本站的 Noto Sans TC Variable，108 個切片中只載入了頁面用到的 18 個 |
| Modal 外觀 | 遮罩 `rgba(15, 16, 18, 0.85)` 加 `blur(8px)` 且覆蓋全螢幕；容器 `rgba(22, 24, 28, 0.95)`，四邊 `1px solid #2A2D34`；左上角 10px 的 `[ SYSTEM_PROMPT ]` |
| Modal 互動 | 開啟後焦點自動進入；Tab 與 Shift+Tab 在對話框內循環；Esc 關閉且焦點回到「開啟 Modal」；點遮罩關閉；背景捲動在開啟時鎖定、關閉後恢復；不可關閉的版本對 Esc 與點遮罩都沒有反應 |
| Toast | 起點在下方、逐格上升並淡入；左側 2px `#FF6B35`；距右下角各 16px；三則依序堆疊且不重疊；約 4 秒後自動消失；`duration: 0` 的不消失，按 × 可關閉；錯誤使用 `role="alert"`；含 HTML 標記的文字以原樣顯示，沒有被解析 |
| 打字機 | 逐字增加（0.3 秒約 15 字、0.7 秒約 37 字，共 58 字）；顯示期間有游標；完成後內容與原文逐字相同（含中文、表情符號、換行）；模擬串流結束後內容相同；略過動畫立刻顯示全部；取消後不再增加；每一刻都停在字素邊界 |
| 網路與主控台 | 整個過程 28 個請求全部指向本站，沒有外部請求；沒有 4xx 或 5xx；主控台沒有錯誤或警告 |

驗收中發現並修正的問題：打字機原本調速過快（一次到達的 58 字不到 0.6 秒就全部出現，幾乎沒有逐字效果），且 `finish()` 的補完期限對一次到達的文字也生效；已調整為基本速度 40 字／秒、加速係數 0.2、補完期限 3 秒（58 字約 1.3 秒、300 字約 2.7 秒）。

**仍需你目視確認**：整體質感與美感是否符合你想要的電影感。可執行 `flask run` 後開啟 `/dev/components` 自行操作。

## 容器建置與驗收

正式環境以 Docker 映像部署（見 AGENTS.md 第 9 節）。映像以 `python:3.14-slim-bookworm` 為基底（Python 版本與 `.python-version` 一致；明確指定 Debian 12，因為 Microsoft ODBC Driver 18 官方支援它），內含 `unixodbc` 與 `msodbcsql18`、前端資產與 108 個字型切片，約 277 MB。

| 面向 | 規則 |
|---|---|
| 執行方式 | `gunicorn`（`gthread`），設定在 `gunicorn.conf.py`；**不使用** Flask 開發伺服器 |
| 初始設定 | 2 個 worker、每個 8 條執行緒、worker 逾時 60 秒、終止時最多等 30 秒讓進行中的請求完成；可用 `WEB_CONCURRENCY`、`GUNICORN_THREADS`、`PORT` 覆寫。數值是初始值，C-5 依負載測試調整 |
| 使用者 | 非 root（uid 10001） |
| 機密 | 映像內沒有 `.env`、`.git`、測試與文件（由 `.dockerignore` 排除，連已刪除的檔案在任何一層都找不到）；所有設定與機密只來自環境變數 |
| 缺少機密 | 使用 `--preload`，設定驗證錯誤會讓主程序以非零碼結束，輸出只列出缺漏的變數名稱 |
| 日誌 | 請求日誌由應用程式輸出 JSON；gunicorn 的存取日誌關閉（避免記下查詢字串） |
| 資料庫升級 | 映像內含 `alembic.ini` 與 `migrations/`，升級是獨立步驟：`docker run --rm -e APP_ENV=... -e DATABASE_URL=... <映像> alembic upgrade head`（應用程式啟動時不會自動升級） |

### 建置與驗收

Docker 在 WSL 內，每次使用前先啟動服務（見下一節）。驗收腳本會建置映像並檢查 56 個項目：

```powershell
.\scripts\verify-container.ps1               # 建置並驗收
.\scripts\verify-container.ps1 --no-build    # 只驗收既有映像
```

檢查內容：Docker 可用、映像建置成功、預設使用者不是 root、映像每一層都沒有 `.env`／`.git`／測試／文件、前端資產與字型齊全、Python 版本等於 `.python-version`、`pyodbc` 可載入且有 ODBC Driver 18、缺少必要機密時以非零碼結束且不洩漏機密值、未設定 `APP_ENV` 時拒絕啟動；並以 `testing` 與「正式環境設定（SQLite 記憶體）」各啟動一次，確認 `/healthz` 與 `/readyz` 回 200、首頁與靜態檔可取得、**正式環境沒有 `/dev/components`**、主行程是非 root 的 gunicorn、日誌是 JSON 且不含機密、收到終止訊號後以結束碼 0 正常結束。

**會員流程**（C-2 新增，在容器內以 SQLite 檔案資料庫執行）：`argon2`、`PyJWT`、`cryptography` 可載入；`alembic upgrade head` 成功；模擬入口層（`X-Forwarded-Proto: https`）後經 HTTP 走完註冊、登入、`/api/auth/me`、`/account`、缺少 CSRF 標頭被拒絕、登出、登出後 401、匿名開啟 `/account` 導向登入並保留 `next`；登入與 CSRF 的 cookie 具備 `__Host-` 前綴、`Secure`、`HttpOnly`、`SameSite=Lax`、`Path=/` 且沒有 `Domain`；日誌沒有信箱、密碼與機密，並有註冊、登入、登出的安全事件。production 的設定驗收也同步更新：`SECRET_KEY` 至少 32 字元，且預設要求信箱驗證，因此需要 https 的 `BASE_URL` 與寄信設定。

**年齡確認**（age-gate 新增，2026-10-28 重新建置並驗收，**67 / 67 項通過**，映像 271 MB）：`alembic upgrade head` 升級到 `0003_users_age_confirmation`；缺少 `confirm_adult` 的註冊被拒絕（400，欄位 `confirm_adult`）；註冊時確認的帳號 `/api/auth/me` 回 `age_confirmed: true` 且不含確認時間與政策版本；`/api/auth/confirm-adult` 缺少 CSRF 標頭為 403、未登入為 401、`confirm_adult` 不是明確的 `true` 為 400。容器內沒有會呼叫 Gemini 的端點可直接驗閘門，所以改以 `flask create-admin`（刻意不代為確認年齡）建立一個「尚未確認」的帳號：登入後 `age_confirmed` 為 `false`，補確認後為 `true` 並在同一個登入狀態中維持；日誌有 `auth.age_confirmed.register` 與 `auth.age_confirmed.later`，且沒有信箱、密碼與機密。

不需要 Docker 的靜態檢查（`tests/test_container_files.py`）也包含在一般的 `pytest` 中。

### 已知限制

- **容器連不到本機 SQL Server**：本機 SQL Server 只監聽 `127.0.0.1`，WSL 內的容器無法連線，所以容器的執行行為以 **SQLite** 驗收；真實的 SQL Server 連線由主機上的整合測試（`pytest -m sqlserver`）驗證。我們**不**為此放寬 SQL Server 的網路綁定。容器內的 SQL Server 驅動「實際連線」要到 C-5 的雲端環境才會首次驗證（本機只確認驅動已安裝且可載入）。
- 終止訊號的驗收確認「正常結束（結束碼 0）並依序關閉」，**沒有**測試「進行中的慢請求會被等完」（目前沒有慢的端點可用來測）。
- 映像只建置 `amd64`。
- 舊版 WSL 不支援 systemd，Docker 需要手動啟動，且每次重新開機後都要再啟動一次。

## 在 WSL 內使用 Docker

本機沒有 Windows 原生的 `docker` 指令。Docker Engine 安裝在 WSL 發行版 `Ubuntu-2204` 內；舊版 WSL 不支援 systemd，所以每次使用前要先啟動服務（可重複執行）：

```powershell
wsl -d Ubuntu-2204 -u root -- service docker start
wsl -d Ubuntu-2204 -u root -- docker ps
```

專案目錄在 WSL 內的路徑是 `/mnt/c/Users/User/Desktop/newprojet`。

## 規範摘要（細節見 AGENTS.md）

- 前端完全離線：不使用 CDN、Google Fonts、`.vue`、Vite 或 Bootstrap 的 JavaScript。
- 資料存取一律透過 SQLAlchemy ORM，不寫原生 SQL。
- 機密（含資料庫密碼）只放在不進版本庫的 `.env`，絕不寫入程式碼或文件。
- 全系統使用繁體中文與 UTF-8。
