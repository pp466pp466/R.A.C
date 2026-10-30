# 帳號、年齡與存取控制

> 狀態：已實作（管理員後台由 C-4b 實作；使用者自助刪除帳號與受限登入狀態由 C-4c 實作） ｜ 最後更新：2026-10-30 ｜ 對應 change：member-auth、age-gate、age-verification-birthdate、cs-chat-core（`chat_gate`）、admin-console（管理員檢查、重新輸入密碼）、openai-compatible-provider（等級 3 的環境開關）、privacy-lifecycle（自助刪除帳號、受限登入狀態） ｜ 規範依據：AGENTS.md 第 10、11 節

本文件說明會員如何註冊、驗證、登入，年齡如何決定能做什麼，權限如何集中判斷，以及管理員後台的存取規則。端點的完整清單見本文第 6 節（會員）與第 7 節（後台）；請求本身的處理順序見 [系統架構-architecture.md](系統架構-architecture.md)。

## 1. 帳號狀態

```mermaid
stateDiagram-v2
    [*] --> pending_verification : 註冊（信箱驗證開啟）
    [*] --> active : 註冊（信箱驗證關閉）或 flask create-admin
    pending_verification --> active : 信箱驗證完成
    active --> suspended : 管理員停權（token_version 加 1）
    pending_verification --> suspended : 管理員停權
    suspended --> active : 管理員解除（信箱驗證開啟而從未驗證者回 pending_verification）
    active --> deletion_pending : 使用者申請刪除（token_version 加 1，所有裝置登出）
    pending_verification --> deletion_pending : 使用者申請刪除
    deletion_pending --> active : 7 天內撤銷（申請前為 active）
    deletion_pending --> pending_verification : 7 天內撤銷（申請前為 pending_verification）
    deletion_pending --> [*] : 寬限期滿由 execute-account-deletions 抹除
    active --> [*] : 連續 12 個月未登入直接刪除（管理員不納入）
```

- `active` 與 `pending_verification` 的帳號可登入；`suspended` 一律拒絕登入。`deletion_pending` **可以登入，但只得到受限登入狀態**（只能撤銷刪除與登出，見第 7a 節）。信箱驗證開啟時，信箱必須已驗證才能使用客服。
- 停權的帳號無法登入，所以無法自助刪除（仍由 12 個月閒置清除處理）；管理員帳號不可自助刪除。
- `EMAIL_VERIFICATION_REQUIRED`：development、testing、staging 可關閉（註冊後直接 `active`、忘記密碼停用）；production 必須開啟並設定寄信服務，否則啟動失敗。

## 2. 註冊與登入

```mermaid
sequenceDiagram
    participant U as 使用者
    participant A as /api/auth
    participant S as AuthService
    participant D as 資料庫
    participant M as 寄信（延後動作）

    U->>A: POST register（email、密碼、生日、年齡區間、同意條款）
    A->>S: register()
    S->>S: 驗證密碼長度 8 到 128、年齡區間與生日對照
    alt 未滿 13 歲或對不起來
        S-->>U: 400（不建立帳號、不保存生日）
    else 通過
        S->>D: 寫入 users（Argon2id、email_canonical、年齡紀錄）
        S-->>U: 200 通用訊息（信箱是否已存在，回應完全相同）
        S->>M: 寄驗證信（提交後才寄）
    end
    U->>A: POST login
    A->>S: login()
    alt 帳號不存在、密碼錯誤、被鎖定
        S-->>U: 401 同一則通用訊息（運算量相當）
    else 通過
        S->>D: 重設失敗計數、記錄最後登入
        A-->>U: 設定簽章 session cookie 並換發 CSRF 權杖
    end
```

| 項目 | 規則 |
|---|---|
| 密碼 | Argon2id，長度 8 至 128 字，不強制字元組合 |
| 防帳號列舉 | 註冊、忘記密碼、登入失敗的回應一律相同 |
| 登入失敗 | 同一帳號連續 5 次鎖定 15 分鐘，計數存資料庫 |
| 信箱權杖 | 32 位元組隨機值，只存 SHA-256；驗證信 24 小時、重設信 1 小時；單次使用；兩次寄信至少間隔 60 秒並有每小時上限 |
| 連結流程 | GET 只顯示確認頁，POST 才完成動作；權杖放在網址片段（`#token=`），不進存取日誌 |
| 重設密碼成功 | `token_version` 加 1（撤銷既有登入）並寄通知信 |

## 3. Session 與 CSRF

```mermaid
flowchart LR
    Login["登入成功"] --> Cookie["簽章 session cookie<br/>內容只有 uid 與 tv（token_version）<br/>HttpOnly、SameSite=Lax、7 天（不延長）"]
    Cookie --> Resolve["每個請求：current_user()<br/>比對 token_version 與帳號狀態"]
    Resolve -->|"不符或停權"| Clear["清除 cookie，視為未登入"]
    Page["頁面載入"] --> Meta["meta csrf-token"]
    Meta --> Header["axios 預設帶 X-CSRF-Token"]
    Header --> Check["非 GET 請求：驗證權杖 + Origin"]
    Check -->|"失敗"| E403["403 csrf_failed"]
```

- staging 與 production 的 cookie 使用 `__Host-` 前綴（Secure、Path=/、無 Domain）；入口層由 `ProxyFix` 信任轉發標頭。
- 改密碼、登出所有裝置、重設密碼、綁定 Google 時 `token_version` 加 1，既有登入立即失效。

## 4. Google 登入（選用）

```mermaid
sequenceDiagram
    participant B as 瀏覽器
    participant A as /api/auth/google/start
    participant G as Google
    participant C as /auth/google/callback
    participant S as GoogleSignInService

    B->>A: POST（同意條款、首次註冊須帶生日與年齡區間）
    A-->>B: 設定 oauth_tx cookie（PKCE verifier、state、nonce）並回授權網址
    B->>G: 授權（Authorization Code + PKCE S256）
    G-->>B: 導回 callback（code、state）
    B->>C: GET
    C->>S: complete()
    S->>G: 以 code 換 token
    S->>S: 驗證 ID token 的 iss、aud、exp、nonce，且 email_verified 為真
    alt 信箱已有本機帳號
        S->>S: 已驗證直接綁定，未驗證則清除未驗證的密碼憑證後綁定並視為已驗證
    else 全新信箱
        S->>S: 建立帳號（年齡規則同註冊）
    end
    C-->>B: 登入並導向（next 由伺服器驗證為站內位置）
```

Google 與信箱密碼共用同一個帳號；開發與正式環境使用不同的 OAuth 用戶端；失敗一律導回 `/login?error=<固定代碼>`，Google 回傳的任何文字都不會進入網址或頁面。

## 5. 年齡閘門與內容上限

年齡規則的所有數字（最低註冊年齡 13、最低使用年齡 13、成年門檻 18、區間選項、政策版本）只在 `app/services/age_policy.py` 的 `AgePolicy`；年齡一律由生日於「當下」計算，不用儲存的結果判斷。

```mermaid
flowchart TD
    Start["會呼叫模型的端點<br/>先問 AccessPolicy.chat_gate(user)"] --> Usable{"帳號 active<br/>且（信箱驗證關閉或信箱已驗證）？"}
    Usable -->|"否"| Forbidden["forbidden（403）<br/>不引導補填年齡"]
    Usable -->|"是"| HasAge{"已完成年齡確認<br/>（有生日）？"}
    HasAge -->|"否"| NeedAge["age_confirmation_required（403）<br/>引導到 /account 補填"]
    HasAge -->|"是"| Old{"年齡 ≥ 最低使用年齡？"}
    Old -->|"否"| Restricted["age_restricted（403）<br/>補填也沒有用，不引導"]
    Old -->|"是"| Pass["通過"]
    Pass --> Ceiling{"年齡 ≥ 18？"}
    Ceiling -->|"否"| Safe["content_ceiling：安全模式<br/>sexual_content_level = 0"]
    Ceiling -->|"是"| Switch{"等級 3 開關開啟？<br/>SEXUAL_CONTENT_LEVEL3_ENABLED"}
    Switch -->|"否（預設）"| Adult2["content_ceiling：最高 2"]
    Switch -->|"是（只限 development 與 testing）"| Adult3["content_ceiling：最高 3"]
```

- `chat_gate` 是純查詢（不拋例外、不記錄事件）；`require_chat_access` 以它判斷並對年齡拒絕記錄一筆事件，兩者不會分歧（測試逐一比對）。
- 管理員與各會員等級都不豁免年齡閘門。管理員帳號由指令建立，沒有生日，第一次登入後須到 `/account` 補填。
- 13 至 17 歲可以使用（含 AI 功能）但固定安全模式；此設定以供應商同意為前提，換供應商時只需換 `AgePolicy` 並提高版本（見 [路線圖-roadmap.md](路線圖-roadmap.md) 的待決定事項）。

**等級 3 的環境開關**（AGENTS.md 第 7 節，change `openai-compatible-provider`）：成年者的上限預設是 2，`sexual_content_level 3` 只能以環境設定開啟，**不能在程式裡寫死**。`AccessPolicy` 的建構子接收 `adult_ceiling`（2 或 3，預設 2），由組裝處依 `SEXUAL_CONTENT_LEVEL3_ENABLED` 傳入；未成年者、尚未完成年齡確認者、管理員與各會員等級都不受開關影響（未成年永遠是安全模式）。

| 環境 | 開關 | 條件 |
|---|---|---|
| development、testing | 可開啟 | 必須已設定 RPG 用途，且 `SEXUAL_CONTENT_LEVEL3_MODELS`（取得書面許可所涵蓋的模型）非空並涵蓋 RPG 用途的主要與備援模型，否則啟動失敗 |
| staging、production | **開啟即啟動失敗** | 強化的成年驗證尚未完成（AGENTS.md：第 10 節訂定驗證做法之前不得對一般會員開啟）；之後做成年驗證的 change 才放寬這個檢查 |

- 清單只在開關開啟時才驗證，關閉時完全不影響行為；它是「防止換了模型卻忘了許可範圍」的護欄，不是許可本身的證明。**許可涵蓋的供應商與日期須由人另外記錄**（使用者尚未提供，見 [路線圖-roadmap.md](路線圖-roadmap.md)）。
- **這只是開關**：RPG 尚未實作，目前沒有任何送出露骨內容的入口。任何入口出現之前，仍須先完成**自家審查層**（擋未成年、性暴力與非自願、以真人名義的內容，不得僅依賴模型拒絕）與**強化成年驗證**（方式尚未選定）。

## 6. 權限規則與端點清單

權限判斷只在 `AccessPolicy`（`app/services/access_policy.py`），端點與其他 Service 不自行判斷：

| 規則 | 行為 |
|---|---|
| 非擁有者存取資源 | 一律 `NotFound`（404），與不存在的回應逐字相同 |
| 會員等級 | 只影響額度，不參與任何權限判斷 |
| 管理員存取他人資料 | 一般端點與一般使用者相同（404）；只有 `/api/admin/*` 能讀他人的內容，走後台專用的 Repository、每次都寫稽核（見第 7 節） |
| 是否為管理員 | `AccessPolicy.require_admin(user)`：不是 `admin` 或帳號不是 `active` 就記錄 `admin.access_denied` 並拋出 `NotFound`（404）；後台的唯一入口 |
| 權限測試 | `tests/support/idor.py` 依序以「他人、不存在、未登入、擁有者」檢查每個帶識別碼的端點 |

`/api/auth/*` 端點（全部需 CSRF；「登入」欄指是否需要已登入）：

| 端點 | 方法 | 登入 | 用途 |
|---|---|---|---|
| `/api/auth/register` | POST | 否 | 註冊（生日與年齡區間必填） |
| `/api/auth/login` | POST | 否 | 登入 |
| `/api/auth/logout` | POST | 否 | 登出目前裝置 |
| `/api/auth/logout-all` | POST | 是 | 登出所有裝置 |
| `/api/auth/me` | GET | 是 | 本人資料（不含生日與內部編號） |
| `/api/auth/verify-age` | POST | 是 | 補填生日與年齡區間 |
| `/api/auth/change-password` | POST | 是 | 變更密碼 |
| `/api/auth/verify-email` | POST | 否 | 以權杖驗證信箱（驗證關閉時 404） |
| `/api/auth/resend-verification` | POST | 是 | 重寄驗證信（驗證關閉時 404） |
| `/api/auth/forgot-password` | POST | 否 | 忘記密碼（驗證關閉時 404） |
| `/api/auth/reset-password` | POST | 否 | 以權杖設定新密碼（驗證關閉時 404） |
| `/api/auth/google/start` | POST | 否 | 發起 Google 登入（未設定時 404） |

`/api/account/*` 端點（C-4c；全部需 CSRF，回應 `no-store`）：

| 端點 | 方法 | 登入 | 用途 |
|---|---|---|---|
| `/api/account/delete` | POST | 是 | 申請刪除自己的帳號（有密碼者帶密碼，只靠 Google 者帶確認文字）；成功後所有裝置登出 |
| `/api/account/cancel-deletion` | POST | 是（含受限狀態） | 寬限期內撤銷刪除，還原為申請前的狀態 |
| `/api/account/export` | POST | 是 | 匯出自己的帳號資料與全部對話（JSON 或 CSV zip）；受限狀態不可用 |

頁面：`/login`、`/register`、`/forgot-password`、`/reset-password`、`/verify-email`、`/account`、`/terms`、`/privacy`、`/auth/google/callback`。

## 7. 管理員後台的存取（C-4b）

後台是高權限的攻擊面，所以重點是**可讀範圍小、每個操作可追溯、危險操作要重新輸入密碼**。業務邏輯在 `AdminService`，控制器只呼叫它（分層規則見 [系統架構-architecture.md](系統架構-architecture.md) 的第 2 節）。

```mermaid
flowchart TD
    Req["請求 /admin 或 /api/admin/*"] --> Login{"已登入？"}
    Login -->|"否"| L401["API：401<br/>頁面：導向 /login?next=/admin"]
    Login -->|"是"| Admin{"require_admin<br/>角色 admin 且帳號 active？"}
    Admin -->|"否"| N404["404（與不存在的路徑逐字相同）<br/>記錄 admin.access_denied（只有內部編號）"]
    Admin -->|"是"| Kind{"操作種類"}
    Kind -->|"讀取：總覽、清單、查詢、對話全文、失敗原文"| Read["先寫稽核再回傳內容<br/>稽核寫入失敗就不回傳"]
    Kind -->|"危險：停權、解除、調整等級、刪除對話"| Pw["confirm_password<br/>管理員自己的密碼"]
    Pw -->|"錯誤或鎖定"| Fail["400，欄位 password<br/>寫 reauth.failed 並保存"]
    Pw -->|"通過"| Do["條件式 UPDATE 或刪除<br/>稽核與動作同一個交易"]
```

| 規則 | 行為 |
|---|---|
| 非管理員 | 一律 404，不是 403（否則會告訴一般使用者後台存在）；順序是「登入 → 管理員檢查 → 才解析請求本文」，所以格式錯誤的本文也只得到 404 |
| 可讀範圍 | 清單沒有訊息內容；只有「意見回饋」（助理回覆的意圖是 `complaint_feedback` 或 `human_handoff`）與「被內容安全過濾標記」兩類對話可讀全文，不符資格與不存在得到同一個 404；使用者資料沒有任何個別的年齡資料，年齡只有總覽的區間人數 |
| 找使用者 | 主要入口是警告（總覽的今日警告 → 被標記對話 → 看使用者）；信箱精確查詢是次要入口、供客服反應時使用；沒有使用者清單與模糊搜尋 |
| 重新輸入密碼 | `AuthService.confirm_password`：與登入共用失敗計數與鎖定（連續 5 次鎖定 15 分鐘），鎖定中即使正確也拒絕且訊息相同；成功不重設計數；沒有設定密碼的管理員不能執行（`no_password`） |
| 操作對象 | 只能是一般使用者：管理員（含自己）403 `admin_target_forbidden`；`deletion_pending` 409；兩位管理員同時操作靠條件式 UPDATE 不互相覆蓋 |
| 稽核 | `admin_audit_logs`：8 個固定動作，只存對外識別碼、管理員填寫的原因與列舉細節，不含 email 與訊息；Repository 沒有更新與刪除（見 [資料模型-data-model.md](資料模型-data-model.md)） |
| 回應 | `/admin` 與 `/api/admin/` 一律 `Cache-Control: no-store`；非 GET 須通過 CSRF |

`/api/admin/*` 端點（全部需要登入且是管理員；`POST` 另需 CSRF）：

| 端點 | 方法 | 用途 |
|---|---|---|
| `/api/admin/overview` | GET | 總覽與今日警告的三個計數（只有彙總） |
| `/api/admin/audit` | GET | 稽核紀錄，由新到舊 |
| `/api/admin/users/lookup` | POST | 以信箱查詢使用者（次要入口，信箱在本文） |
| `/api/admin/users/<public_id>` | GET | 以對外識別碼檢視使用者 |
| `/api/admin/users/<public_id>/suspend` | POST | 停權（原因與密碼） |
| `/api/admin/users/<public_id>/unsuspend` | POST | 解除停權（密碼） |
| `/api/admin/users/<public_id>/tier` | POST | 調整會員等級（密碼） |
| `/api/admin/feedback` | GET | 意見回饋對話清單 |
| `/api/admin/flagged` | GET | 被標記對話清單（帶今日被擋次數與暫停截止時間） |
| `/api/admin/conversations/<public_id>` | GET | 讀取可讀對話的全文（最多 200 則） |
| `/api/admin/conversations/<public_id>/delete` | POST | 刪除整個對話（原因與密碼） |
| `/api/admin/failures` | GET | 最近 14 天模型輸出驗證失敗的原文 |

頁面：`/admin`（只有管理員的顯示名稱，所有資料經由 API 取得；前端見 [前端結構-frontend.md](前端結構-frontend.md)）。

已知取捨：被盜用的管理員登入狀態可以不輸入密碼讀取兩類對話（每次都有稽核，要加強只需讓「讀全文」也要求密碼）；管理員輸入錯誤密碼也累積登入的失敗計數，連續 5 次會把自己鎖定 15 分鐘（刻意，讓後台不成為暴力猜測的捷徑）。完整清單見 README 的「管理員後台（C-4b）」。

## 7a. 受限登入狀態與自助刪除（C-4c）

使用者可以自助刪除帳號，並有 7 天寬限期可以撤銷。難處在「寬限期內帳號已停用，卻還要讓本人能撤銷」：做法是讓待刪除的帳號**可以登入**，但只得到**受限登入狀態**，並在入口層**預設拒絕**。

```mermaid
sequenceDiagram
    participant U as 使用者
    participant A as /api/account
    participant S as AccountDeletionService
    participant D as 資料庫
    participant M as 寄信（延後動作）
    participant J as 排程指令

    U->>A: POST delete（有密碼：password；只靠 Google：confirm_text）
    A->>S: request()
    S->>S: 管理員 403；狀態須為 active 或 pending_verification
    S->>S: 確認本人（confirm_password，與登入共用失敗計數與鎖定；或固定確認文字）
    S->>D: 條件式 UPDATE：deletion_pending、申請時間、申請前狀態、token_version 加 1
    S->>M: 通知信（提交之後才寄，失敗不影響結果）
    A-->>U: 200，清除這台的 cookie（其他裝置因 token_version 失效）
    U->>A: POST login（密碼或 Google）
    A-->>U: 登入成功，狀態為 deletion_pending（受限登入狀態），導向 /account
    U->>A: POST cancel-deletion
    A->>S: cancel()
    S->>D: 條件式 UPDATE：申請未滿 7 天才還原為申請前的狀態
    A-->>U: 200，目前的登入狀態立刻變成完整的（token_version 不變）
    J->>D: 寬限期已滿的待刪除帳號 → 抹除（AccountErasureService）
```

```mermaid
flowchart TD
    Req["需要登入的請求"] --> Who{"require_login / login_required_page"}
    Who -->|"未登入"| A401["API 401；頁面導向 /login"]
    Who -->|"受限登入狀態（deletion_pending）"| Allowed{"端點明確允許？"}
    Allowed -->|"否（預設）"| Deny["API 403 account_deletion_pending；頁面導向 /account"]
    Allowed -->|"是：/api/auth/me、/api/account/cancel-deletion、/account"| Ok["放行"]
    Who -->|"一般登入狀態"| Ok
```

| 規則 | 行為 |
|---|---|
| 受限登入狀態預設拒絕 | `resolve_session` 對 `deletion_pending` 回傳使用者；`require_login(allow_pending_deletion=False)` 與 `login_required_page` 預設拒絕它。**既有端點不必修改**就自動被擋，新增的端點漏掉檢查也是安全的一邊。`tests/test_restricted_session.py` 掃描 `app.url_map` 的每條規則：先以未登入找出需要登入的端點，再以受限登入狀態逐一請求，允許清單以外必須全部被擋，並有自我驗證（暫時加一個只用 `current_user()` 的端點必須被抓到） |
| 第二道防線 | `AccessPolicy.chat_gate` 與 `is_admin` 各自要求帳號為 `active`，所以即使入口層漏了，客服與後台仍然不可用 |
| 確認本人 | 有密碼的帳號重新輸入密碼（沿用 `AuthService.confirm_password`，連續 5 次失敗鎖定 15 分鐘）；沒有密碼（只靠 Google）的帳號輸入固定文字「刪除我的帳號」（NFC 正規化並去除前後空白，含全形空白）。伺服器依帳號是否有密碼決定要哪個欄位，另一個欄位被忽略 |
| 申請 | 單一條件式 `UPDATE`：非管理員、狀態為 `active` 或 `pending_verification`；同一個語句記下申請時間與申請前的狀態並使 `token_version` 加 1。兩個並行的申請只有一個成立 |
| 撤銷 | 單一條件式 `UPDATE`：狀態為 `deletion_pending` 且申請未滿 7 天；還原為申請前的狀態（待驗證信箱的帳號仍待驗證，除非待刪除期間已經驗證）。超過 7 天回 409 `grace_expired`，帳號維持待刪除 |
| 期滿的抹除 | `flask execute-account-deletions`：條件在刪除當下再確認一次，剛撤銷的帳號不會被刪；每個帳號一個交易，原因 `user_requested` |
| Google 登入 | 已綁定 Google 的待刪除帳號可登入（受限狀態，導向 `/account`）；以信箱找到、尚未綁定的待刪除帳號仍拒絕，不在即將刪除的帳號上新增綁定 |
| 忘記密碼 | 待刪除期間維持停用（`EmailFlowService` 的 `_BLOCKED_FROM_RESET`）：有密碼者申請時才輸入過密碼，Google 登入或等候期滿都能解決 |
| 帳號頁 | 待刪除者拿到**另一份 HTML**（預定刪除時間、撤銷、登出，沒有其他區塊；寬限期已過就不顯示撤銷按鈕）；管理員的頁面沒有刪除區塊 |
| 匯出 | 受限狀態不能匯出：請在申請刪除之前匯出。內容與格式見 [資料模型-data-model.md](資料模型-data-model.md) 與 README |

## 8. 個資與保存

- 生日與年齡區間：不進 API 回應、日誌與模型；管理員只看年齡區間的彙總統計（`flask age-stats` 與後台總覽），後台看不到任何個別使用者的生日、年齡區間或年齡群組。
- 連續 12 個月未登入的帳號由 `flask purge-inactive-accounts` 直接刪除（管理員不納入）；使用者自助刪除寬限期滿後由 `flask execute-account-deletions` 刪除。兩者走同一個抹除出口（先清帳本的對話編號，再刪使用者，並寫入只含內部編號、日期與原因的 `deletion_tombstones`，見 [資料模型-data-model.md](資料模型-data-model.md)）。各類資料的保存期限由 `flask purge-expired-data` 清除。
- 使用者可在帳號頁匯出自己的帳號資料（含生日與年齡區間）與全部對話；匯出不含密碼雜湊、權杖與模型內部欄位，CSV 的公式字元會被中和。
- 日誌只記錄白名單欄位，絕不記錄 email、密碼、權杖、生日、訊息全文與模型輸出（見 [營運與部署-operations.md](營運與部署-operations.md)）。

---

**何時更新本文**：註冊、登入、驗證、Google 登入、session、CSRF、年齡規則、權限政策、`/api/auth/*` 與 `/api/admin/*` 端點、後台的可讀範圍與重新輸入密碼規則、帳號狀態或保存期限改變時。`tests/test_design_docs.py` 會檢查每個 `/api/auth/` 與 `/api/admin/` 路由都出現在本文。
