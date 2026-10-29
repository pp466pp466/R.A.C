# 帳號、年齡與存取控制

> 狀態：已實作（帳號刪除與管理員後台由 C-4 實作） ｜ 最後更新：2026-10-29 ｜ 對應 change：member-auth、age-gate、age-verification-birthdate、cs-chat-core（`chat_gate`） ｜ 規範依據：AGENTS.md 第 10、11 節

本文件說明會員如何註冊、驗證、登入，年齡如何決定能做什麼，以及權限如何集中判斷。端點的完整清單見本文第 6 節；請求本身的處理順序見 [系統架構-architecture.md](系統架構-architecture.md)。

## 1. 帳號狀態

```mermaid
stateDiagram-v2
    [*] --> pending_verification : 註冊（信箱驗證開啟）
    [*] --> active : 註冊（信箱驗證關閉）或 flask create-admin
    pending_verification --> active : 信箱驗證完成
    active --> suspended : 管理員停權（C-4）
    suspended --> active : 管理員解除（C-4）
    active --> deletion_pending : 使用者申請刪除（C-4）
    deletion_pending --> active : 7 天內撤銷
    deletion_pending --> [*] : 期滿由排程刪除
    active --> [*] : 連續 12 個月未登入直接刪除（管理員不納入）
```

- 只有 `active` 的帳號能登入；信箱驗證開啟時，信箱必須已驗證才能使用客服。
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
    Ceiling -->|"是"| Adult["content_ceiling：最高 2<br/>（3 在取得供應商書面許可與法務意見前鎖定）"]
```

- `chat_gate` 是純查詢（不拋例外、不記錄事件）；`require_chat_access` 以它判斷並對年齡拒絕記錄一筆事件，兩者不會分歧（測試逐一比對）。
- 管理員與各會員等級都不豁免年齡閘門。管理員帳號由指令建立，沒有生日，第一次登入後須到 `/account` 補填。
- 13 至 17 歲可以使用（含 AI 功能）但固定安全模式；此設定以供應商同意為前提，換供應商時只需換 `AgePolicy` 並提高版本（見 [路線圖-roadmap.md](路線圖-roadmap.md) 的待決定事項）。

## 6. 權限規則與端點清單

權限判斷只在 `AccessPolicy`（`app/services/access_policy.py`），端點與其他 Service 不自行判斷：

| 規則 | 行為 |
|---|---|
| 非擁有者存取資源 | 一律 `NotFound`（404），與不存在的回應逐字相同 |
| 會員等級 | 只影響額度，不參與任何權限判斷 |
| 管理員存取他人資料 | 一般端點與一般使用者相同（404）；將來走專用 Repository 方法並寫稽核紀錄（C-4） |
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

頁面：`/login`、`/register`、`/forgot-password`、`/reset-password`、`/verify-email`、`/account`、`/terms`、`/privacy`、`/auth/google/callback`。

## 7. 個資與保存

- 生日與年齡區間：不進 API 回應、日誌與模型；管理員只看年齡區間的彙總統計（`flask age-stats`）。
- 連續 12 個月未登入的帳號由 `flask purge-inactive-accounts` 直接刪除（管理員不納入），並寫入只含內部編號與日期的 `deletion_tombstones`（見 [資料模型-data-model.md](資料模型-data-model.md)）。
- 日誌只記錄白名單欄位，絕不記錄 email、密碼、權杖、生日、訊息全文與模型輸出（見 [營運與部署-operations.md](營運與部署-operations.md)）。

---

**何時更新本文**：註冊、登入、驗證、Google 登入、session、CSRF、年齡規則、權限政策、`/api/auth/*` 端點、帳號狀態或保存期限改變時。`tests/test_design_docs.py` 會檢查每個 `/api/auth/` 路由都出現在本文。
