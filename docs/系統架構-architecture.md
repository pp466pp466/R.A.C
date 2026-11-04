# 系統架構與分層

> 狀態：已實作（RPG 為規劃中） ｜ 最後更新：2026-11-03 ｜ 對應 change：rpg-data-and-script、remove-player-password-auth、platform-foundation、member-auth、cs-chat-core、usage-limits、admin-console、openai-compatible-provider、privacy-lifecycle、ops-alerting、launch-readiness、staging-deployment、google-first-login ｜ 規範依據：AGENTS.md 第 3、4、9、10 節
> 注意（2026-11-03）：Google 登入改版的兩個 change 都已實作：change ① `google-first-login`（`AccountLockService`、`AppealService`、`SlidingSessionInterface`、受限登入狀態一般化、管理員二階段登入的中間狀態 cookie）與 change ② `remove-player-password-auth`（移除玩家端密碼註冊、密碼登入、忘記密碼與信箱驗證；`AuthService` 只剩管理員的密碼驗證、補填年齡、變更密碼與建立管理員，`EmailFlowService` 只剩管理員的重設密碼；玩家不收任何信）。
> 注意（2026-11-03）：change `rpg-data-and-script`（RC-1）加入 RPG 的資料基礎：`ScriptRepository`、`SessionRepository`、`ContentModeService`、`SessionService`、`ScriptSeedService` 與純運算的 `script_spec`、`content_tier`；`AccessPolicy` 的內容上限改為三種內容模式。還沒有任何玩家看得到的 RPG 介面。

本文件說明系統由哪些部分組成、後端如何分層、一個請求怎麼走完、外部依賴如何注入。其他設計文件（資料模型、帳號與安全、客服流程、模型呼叫層、前端、營運）都以這裡的分層為準。

## 1. 系統全景

```mermaid
flowchart LR
    Browser["瀏覽器<br/>Vue 3 全域變數模式 + axios<br/>IndexedDB 離線暫存"]
    Edge["入口層（C-5 規劃）<br/>HTTPS、HTTP/2、HSTS、限流"]
    subgraph Container["容器（Cloud Run）"]
        Gunicorn["gunicorn gthread<br/>2 workers x 8 threads"]
        Flask["Flask 應用程式<br/>create_app()"]
    end
    DB[("資料庫<br/>SQL Server / SQLite")]
    Gemini["Gemini API<br/>（過渡期的模型供應商）"]
    Smtp["SMTP<br/>營運告警信與管理員的重設密碼信"]
    Google["Google OIDC<br/>選用的 Google 登入"]
    Secrets["機密注入<br/>環境變數 / Secret Manager"]

    Browser -->|"HTTPS"| Edge --> Gunicorn --> Flask
    Browser -.->|"本機開發直連"| Gunicorn
    Flask -->|"SQLAlchemy ORM"| DB
    Flask -->|"urllib REST（非串流）"| Gemini
    Flask -->|"延後動作寄信"| Smtp
    Flask -->|"授權碼 + PKCE"| Google
    Secrets -.-> Flask
```

- 瀏覽器只載入本機資產（`static/vendor`、`static/fonts`），不使用任何 CDN。
- 所有狀態都在資料庫：多個 worker 之間沒有行程內共用狀態（限流、處理標記、斷路都存資料庫）。
- 模型供應商是可替換的：業務程式只認得 `app/llm/types.py` 的中立型別（見 [模型呼叫層-llm-layer.md](模型呼叫層-llm-layer.md)）。

## 2. 後端分層與依賴方向

```mermaid
flowchart TD
    Pages["controllers：MVC 頁面<br/>只回 HTML 外殼與初始狀態"]
    Api["controllers：Web API<br/>只回 JSON"]
    Sec["security：HTTP 安全輔助<br/>session、CSRF、cookie、重新導向"]
    Svc["services：業務邏輯<br/>AccessPolicy、ChatService、SessionService、UsageService、LlmService、AdminService…"]
    Llm["llm：供應商中立型別與轉接器"]
    Repo["repositories：ORM 存取<br/>條件式 UPDATE、擁有者條件"]
    Model["models：SQLAlchemy 資料模型"]
    Db["db：引擎與請求級 Session"]
    Obs["observability：結構化日誌與事件"]
    Cfg["app/__init__.py + config.py<br/>組裝層，不受限制"]

    Pages --> Svc
    Api --> Svc
    Pages --> Sec
    Api --> Sec
    Sec --> Svc
    Svc --> Repo
    Svc --> Llm
    Svc --> Obs
    Repo --> Model
    Svc --> Model
    Db --> Model
    Cfg --> Pages
    Cfg --> Api
    Cfg --> Db
    Cfg --> Llm
```

`tests/architecture/test_layering.py` 以 AST 檢查「各層禁止匯入」，違反就失敗：

| 層            | 禁止匯入                                           | 原因                                         |
| ------------- | -------------------------------------------------- | -------------------------------------------- |
| controllers   | repositories、db、models、sqlalchemy               | 端點只呼叫 Service，不碰資料庫               |
| security      | repositories、db、models、sqlalchemy               | HTTP 輔助不直接存取資料                      |
| services      | controllers、flask                                 | Service 不知道 HTTP                          |
| repositories  | services、controllers、flask                       | 資料存取不依賴業務邏輯                       |
| models        | services、repositories、controllers、flask         | 模型是最底層                                 |
| db            | services、repositories、controllers                | 資料庫基礎不依賴上層                         |
| observability | services、repositories、controllers                | 日誌可被任何層使用                           |
| llm           | services、repositories、controllers、models、flask | 供應商轉接器不依賴業務層，換供應商只動這一層 |

另有三項架構測試：業務程式碼（services、controllers、repositories、models、security、observability、db）不得含供應商的 SDK、網域與金鑰標頭名稱（`test_no_vendor_in_business_code.py`）；每個指向 `users.id` 的外鍵必須是 `CASCADE` 或可為空的 `SET NULL`（`test_user_foreign_keys.py`）；後台的兩條規則（`test_admin_boundaries.py`，下述）。

### 後台專用的 Repository（C-4b）

一般路徑的 Repository 方法第一個參數必須是 `user_id`（擁有者條件寫進簽名）；管理員要讀他人的對話，**不放寬這條規則**，而是另一組明確標示為後台專用的方法，並用對稱的規則守住：

| 規則 | 守法 |
|---|---|
| `AdminService` 的每個公開方法，第一個陳述式是 `self._policy.require_admin(admin)` | `tests/architecture/test_admin_boundaries.py`（AST；測試本身有自我驗證，餵它故意違規的原始碼必須失敗並指出方法） |
| 後台專用的 Repository（`admin_repository.py`、`admin_audit_repository.py`）只被 `admin_service.py` 與 `registry.py` 匯入 | 同上（指出違規的檔案） |
| `AdminAuditRepository` 只有 `add` 與 `list`，沒有更新與刪除 | `tests/test_admin_audit_repository.py` 以反射檢查公開方法 |

`AdminService` 依賴 `AccessPolicy`、兩個後台 Repository、`UsageService`（用量快照）、`AgeStatsService`（年齡區間人數）與兩個注入的可呼叫物件：`AuthService.confirm_password`（重新輸入密碼）與 `keep_changes_on_error`（失敗的重新驗證也要保存稽核），由 `registry.get_admin_service()` 組裝。回傳的資料一律是明確組出來的字典，不是 ORM 物件，所以內部編號、密碼雜湊與 `token_version` 不可能意外外露。

### 帳號鎖定、申訴與受限登入狀態（google-first-login）

| 元件 | 角色與規則 | 守法 |
|---|---|---|
| `AccountLockService`（`app/services/account_lock_service.py`） | 兩類計次（`identity_rejected` 15 分鐘 5 次、`rate_limited` 15 分鐘 10 次）寫入 `lock_strikes`，達門檻以條件式 UPDATE 鎖定並寫 `account.lock` 稽核（管理員欄位為空）。呼叫點只有三處：Google 回呼的拒絕（`GoogleSignInService._refuse`，已辨識出帳號者）、補填年齡不一致（`AuthService.verify_age`）、被每分鐘訊息數上限擋下（`UsageService._check_rate`）。計次寫入後呼叫 `keep_changes_on_error`，所以 4xx 的請求也保留 | `tests/test_account_lock_service.py`（SQLite 與 SQL Server、並行只鎖一次）；`tests/test_chat_usage_limits.py`、`test_google_endpoints.py` 證明三個呼叫點 |
| 稽核以注入的函式寫入 | `AccountLockService` 與 `AppealService` 拿到的是 `registry._system_audit` 產生的函式，不直接匯入後台專用的 `AdminAuditRepository`，所以後台專用 Repository 仍只被 `admin_service.py` 與 `registry.py` 匯入 | `tests/architecture/test_admin_boundaries.py` |
| `AppealService`（`app/services/appeal_service.py`） | 只有被鎖定的玩家能申訴（其他人 404）；1000 字、每小時 3 則、同時 1 則待處理；內容是個資，稽核與日誌只有字數 | `tests/test_appeal_api.py` |
| 受限登入狀態 | `CurrentUser.restriction` 回傳 `locked`／`age_pending`／`deletion_pending`（依此優先順序），`require_login`／`login_required_page` 預設拒絕，只有明確傳入 `allow_*` 的端點通過；管理員不會是 `locked` 或 `age_pending` | `tests/test_restricted_session.py` 對三種狀態各掃描一次全部路由，並有自我驗證 |
| `SlidingSessionInterface` | session 到期時間一律是「寫入的當下加 30 天」，辨識過登入者的回應才重發 cookie；取代絕對期限的實作 | `tests/test_session_security.py`（第 20 天使用會續期、連續使用 100 天仍有效、閒置 31 天失效） |
| 管理員的中間狀態 cookie | `app/security/admin_step.py`：10 分鐘的簽章 cookie，不被 `current_user()` 辨識；`POST /api/auth/admin-password` 與 `/forgot` 是登入之前的端點，不在受限狀態掃描的範圍（明列於測試） | `tests/test_admin_two_step_login.py` |
| `landing_for(user, next)`（`app/security/redirects.py`） | 首頁、登入頁與 Google 回呼共用：受限狀態各去自己的頁面，待確認年齡帶著已驗證的 `next` 去 `/age`，否則 `next` 或預設頁面 | `tests/test_landing.py` |

### 抹除的單一出口與期限清除的專用 Repository（C-4c）

「刪除一個人」只有一個出口，期限清除的刪除能力只放在一個專用的 Repository，兩者都由架構測試守住：

| 規則 | 守法 |
|---|---|
| 閒置清除、自助刪除、逾時未補填年齡的清除（`purge-unconfirmed-accounts`）與補填時未滿 13 歲的立即抹除，都共用 `AccountErasureService`：先把別人的存檔對這位作者的劇本版本的來源參照清為空值（無動作的外鍵，不清會擋住刪除）、再清帳本的對話編號、再條件式刪除、寫刪除紀錄、提交；條件不成立整個回復 | `tests/test_account_erasure_service.py`；`tests/test_cli_account_retention.py` 證明閒置清除也走它 |
| 每張指向使用者、對話、存檔、劇本或版本的資料表都要登記（任意父表的串聯與無動作的外鍵都分類），沒有外鍵的整數參照與無動作的外鍵必須由抹除流程在刪除使用者之前明確清除 | `tests/architecture/test_account_erasure_coverage.py`（新增資料表漏掉就失敗，並有自我驗證）；`test_account_erasure_rpg_data.py` 以行為證明作者被抹除時別人的存檔保留 |
| 劇本版本的狀態與兩個輔助欄位（`live_script_id`、`open_script_id`）只由 `ScriptRepository` 寫入；不得在別處建立 `ScriptVersion`、對它 `update`／`delete` 或對它的 `status` 賦值 | `tests/architecture/test_script_state_writes.py`（AST 掃描 `app/`，並附反向驗證） |
| `RetentionRepository`（依期限刪除對話、帳本、權杖、稽核、鎖定計次、RPG 存檔與回合的部分內容）只被 `data_retention_service.py` 與 `registry.py` 匯入；`AdminAuditRepository` 仍沒有更新與刪除 | `tests/architecture/test_retention_boundaries.py` |
| 需要登入的端點預設拒絕三種受限登入狀態（等待刪除、待確認年齡、被鎖定），只有明確允許的端點可用 | `tests/test_restricted_session.py` 掃描 `app.url_map` 的每條規則（見 [帳號與安全-auth-security.md](帳號與安全-auth-security.md) 第 7a、7b 節） |

匯出（`PersonalExportService`）只透過 `ChatRepository`、`ScriptRepository`、`SessionRepository` 與 `UserRepository` 讀取，沿用「擁有者條件寫進簽名」的規則（訊息以 `messages_after` 分頁；劇本、存檔與回合以產生器逐筆、逐頁讀取，簽名第一個參數是 `user_id`），不需要新的繞過路徑。

### RPG 資料基礎（rpg-data-and-script）

| 元件 | 責任 |
|---|---|
| `script_spec.py`（純運算） | 固定規格 v1 的驗證與標準化，輸出標準化內容與 `content_hash`；沒有資料庫、網路與時間的依賴 |
| `content_tier.py`（純邏輯） | 三種內容模式的順序與「取較保守者」，無法判定視為 `safe`；不內嵌任何年齡數字 |
| `AccessPolicy.content_access`／`effective_tier` | 依生日於當下計算的年齡、部署開關與成年確認，算出年齡上限與生效模式；管理員與各等級不豁免 |
| `ContentModeService` | 玩家查詢與切換自己的內容模式、成年確認；`GET`／`PUT /api/account/content-mode` |
| `ScriptRepository` | 劇本與版本：擁有者範圍的方法都接收 `user_id`，公開讀取只回傳上架版本；狀態轉移集中在少數方法，核可時取代舊版與鎖定筆名在同一個 SAVEPOINT |
| `SessionRepository`／`SessionService` | 存檔與回合：所有方法接收 `user_id`；開始回合先對存檔列做條件式 UPDATE 取鎖（串行化並行請求）；開存檔先過年齡閘門、檢查來源版本與劇本宣告的內容模式，再複製完整內容 |
| `ScriptSeedService` | 內建劇本的種子（`flask seed-scripts`）：全部驗證通過才在單一交易內寫入，冪等 |

- 開存檔時複製劇本內容，之後存檔不再讀取劇本；`sessions.source_version_id` 是無動作的外鍵，刪除版本與抹除帳號的流程先清為空值。
- 組裝在 `registry.py`（`get_content_mode_service()`、`get_session_service()`、`build_script_seed_service()`）；`AccessPolicy` 的成人模式開關來自 `config.content.adult_mode_enabled`。

## 3. 一個請求的生命週期

```mermaid
sequenceDiagram
    participant C as 瀏覽器
    participant P as ProxyFix（staging 與 production；development 僅在設定 TRUST_PROXY_HEADERS 經 HTTPS 通道時）
    participant L as 請求日誌
    participant G as CSRF 檢查
    participant E as 端點（Controller）
    participant R as registry（組裝）
    participant S as Service
    participant Q as Repository
    participant D as 請求級 Session

    C->>P: HTTP 請求（含 cookie）
    P->>L: 還原協定與主機
    L->>G: 產生 request_id
    G->>E: 非 GET 須通過 Origin 與 X-CSRF-Token
    E->>R: get_chat_service() 等
    R->>S: 以請求的 Session 建立 Service
    E->>S: 呼叫業務方法（登入者 CurrentUser）
    S->>Q: 帶 user_id 的查詢與寫入
    Q->>D: ORM 操作
    S-->>E: 結果或 ServiceError
    E-->>C: JSON 或 HTML（錯誤統一轉成繁體中文）
    Note over D: 狀態碼小於 400 才提交，否則回復
    Note over L: after_request 最後執行，記錄最終狀態碼
    D-->>D: 提交後才執行延後動作（寄信、摘要折疊）
```

重點：

- **Session 只在狀態碼小於 400 時提交**；需要在失敗回應後仍保留的寫入（登入失敗計數）明確呼叫 `keep_changes_on_error()`；需要在呼叫模型前後落地的寫入（客服）由 Service 注入的 `commit` 明確提交。
- **延後動作**（`defer_until_commit`）在提交之後、回應送出之後執行，用自己綁好的資料（寄信、摘要折疊使用自己的 Session）。
- Service 錯誤（`ServiceError` 的子類）統一由 `controllers/errors.py` 轉成狀態碼與 JSON，不含堆疊、SQL 或機密。

## 4. 應用程式工廠與依賴注入

```mermaid
flowchart LR
    Env["環境變數 / .env"] --> Load["load_config()<br/>依 APP_ENV 選設定類別"]
    Load --> Cfg["AppConfig（含 LlmSettings）"]
    Cfg --> Factory["create_app(config, *, mail_sender,<br/>identity_provider, clock, age_policy, llm_provider, rpg_llm_provider)"]
    Test["測試注入<br/>假寄信器、假 Google、假時鐘、假供應商（客服與 RPG 各一）"] -.-> Factory
    Factory --> Ext["app.extensions<br/>app_config、clock、age_policy、mail_sender、<br/>identity_provider、llm_provider、rpg_llm_provider、db_session_factory"]
    Ext --> Runtime["runtime.py<br/>get_config()、get_clock()、get_llm_provider()、get_rpg_llm_provider()…"]
    Runtime --> Registry["services/registry.py<br/>get_*_service()、build_*_service()"]
    Registry --> Services["Service 實例（每個請求一份）"]
```

- 外部依賴（寄信、Google、模型、時鐘、年齡規則）都由 `create_app` 的關鍵字參數注入；沒有注入時依設定建立真實的實作，沒有設定時為空（對應功能回 404 或 503）。
- **模型供應商依用途分開存放**：`llm_provider`（客服與摘要，Gemini）與 `rpg_llm_provider`（RPG 敘事，OpenAI 相容端點）互相獨立；沒有設定 RPG 用途時只有它自己是空的，客服不受影響。`registry.get_llm_service()` 與 `get_rpg_llm_service()` 組出同一個 `LlmService`，只是供應商與設定不同（見 [模型呼叫層-llm-layer.md](模型呼叫層-llm-layer.md) 第 10 節）。
- Service 本身不匯入 flask，也不讀環境變數；命令列指令沒有請求，改用 `build_*` 函式明確傳入 Session。

## 5. 模組與規格、文件對照

| 能力（`openspec/specs/`）                                                                                                                          | 主要程式                                                                                                           | 設計文件                                                                                                   |
| ---------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| application-layering、database-access、app-configuration                                                                                             | `app/__init__.py`、`app/config.py`、`app/db/`、`app/controllers/errors.py`                                 | 本文、[資料模型-data-model.md](資料模型-data-model.md)、[營運與部署-operations.md](營運與部署-operations.md) |
| member-registration、session-authentication、email-token-flows、google-sign-in、csrf-protection、access-control、age-confirmation、account-retention | `app/services/auth_service.py`、`access_policy.py`、`email_flow_service.py`、`google_*`、`app/security/` | [帳號與安全-auth-security.md](帳號與安全-auth-security.md)                                                  |
| cs-conversations、cs-conversation-memory、cs-nlp-contract                                                                                            | `app/services/chat_service.py`、`cs_*.py`、`summary_service.py`、`app/repositories/chat_repository.py`     | [客服對話-chat-service.md](客服對話-chat-service.md)                                                        |
| admin-console、admin-user-management、admin-content-review、admin-audit-log                                                                          | `app/services/admin_service.py`、`app/repositories/admin_*.py`、`app/controllers/admin_*.py`                   | [帳號與安全-auth-security.md](帳號與安全-auth-security.md)、[資料模型-data-model.md](資料模型-data-model.md) |
| security-headers                                                                                                                                     | `app/security/security_headers.py`（HSTS 只在 staging 與 production；不送 CSP） | [營運與部署-operations.md](營運與部署-operations.md) |
| operational-alerts（及 app-configuration、service-observability 的告警條款）                                                                         | `app/services/alert_rules.py`（純函式）、`alert_service.py`、`alert_messages.py`、`app/repositories/alert_state_repository.py`、`app/observability/alert_events.py`、`app/cli.py` 的 `check-alerts` | [營運與部署-operations.md](營運與部署-operations.md)、[資料模型-data-model.md](資料模型-data-model.md) |
| content-tier-modes、script-fixed-spec、script-store、rpg-sessions、builtin-script-seed | `app/services/content_tier.py`、`script_spec.py`、`content_mode_service.py`、`session_service.py`、`script_seed_service.py`、`app/repositories/script_repository.py`、`session_repository.py`、`app/data/scripts/` | [資料模型-data-model.md](資料模型-data-model.md)、[帳號與安全-auth-security.md](帳號與安全-auth-security.md) 第 5 節、[RPG.md](RPG.md) |
| account-deletion、personal-data-export、data-retention                                                                                               | `app/services/account_deletion_service.py`、`account_erasure_service.py`、`account_deletion_executor.py`、`personal_export_service.py`、`data_retention_service.py`、`app/repositories/retention_repository.py`、`app/controllers/account_api_controller.py` | [帳號與安全-auth-security.md](帳號與安全-auth-security.md)、[資料模型-data-model.md](資料模型-data-model.md)、[營運與部署-operations.md](營運與部署-operations.md) |
| llm-provider、llm-structured-calls                                                                                                                   | `app/llm/`、`app/services/llm_service.py`、`llm_output.py`                                                   | [模型呼叫層-llm-layer.md](模型呼叫層-llm-layer.md)                                                          |
| offline-frontend-assets、shared-ui-components、auth-pages、cs-chat-ui                                                                                | `app/static/`、`app/templates/`                                                                                | [前端結構-frontend.md](前端結構-frontend.md)                                                                |
| service-observability、container-runtime                                                                                                             | `app/observability/`、`Dockerfile`、`gunicorn.conf.py`                                                       | [營運與部署-operations.md](營運與部署-operations.md)                                                        |

## 6. 關鍵技術決策

| 決策               | 內容                                                 | 原因                                                        |
| ------------------ | ---------------------------------------------------- | ----------------------------------------------------------- |
| 純 ORM、雙軌資料庫 | 不寫原生 SQL；SQLite 與 SQL Server 行為一致          | 本機開發用 SQLite、正式用 SQL Server；JSON 欄位不做內容查詢 |
| 狀態都在資料庫     | 不依賴行程內記憶體（限流、處理標記、斷路、摘要標記） | 多 worker 與重啟後行為一致                                  |
| 不引入 LLM SDK     | 以標準函式庫 `urllib` 呼叫 REST                    | 減少相依、供應商型別不滲入呼叫端、逾時可控                  |
| 前端不打包         | Vue 3 全域變數模式、不使用 `.vue` 與 Vite          | AGENTS.md 第 2 節；資產全部本地化                           |
| 擁有者條件寫進簽名 | 對話相關的 Repository 方法第一個參數都是 `user_id` | 避免只憑識別碼取資料；AST 測試守住                          |
| 後台走專用路徑     | 管理員讀他人資料只能經 `AdminService` 與後台專用 Repository，每個方法先 `require_admin`、每次讀取先寫稽核 | 不放寬一般路徑的隔離保證；「只有管理員」與「後台資料只走後台路徑」由測試守住，不靠審查 |
| 非擁有者一律 404   | 他人的資源與不存在的回應逐字相同                     | 不洩漏資源是否存在                                          |
| 抹除只有一個出口   | 閒置清除、自助刪除、逾時未補填年齡與未滿 13 歲都走 `AccountErasureService`；新增資料表要登記 | 避免每個功能各做一套刪除而漏掉某張表；帳本的對話編號沒有外鍵，順序錯了就會留下錯指的參照 |
| 受限登入狀態預設拒絕 | 待刪除、待確認年齡與被鎖定的帳號可登入但只能做各自的一小組事情；`require_login` 預設拒絕，允許清單明確列出 | 新增端點漏了檢查也是安全的一邊；以掃描全部路由的測試證明 |

## 7. 規劃中的 RPG 模組

RPG 與客服共用上述分層與模型呼叫層；差異在串流（SSE）、世界狀態與劇本。**資料基礎已實作**（上面的「RPG 資料基礎」）：劇本與版本、存檔與回合、固定規格驗證器、三種內容模式、內建劇本種子。**尚未實作**：創作介面（RC-7）、審核與公開目錄（RC-8）、回合引擎與串流（RC-2）、遊戲介面（RC-3）。細節見 [RPG.md](RPG.md)，進度見 [路線圖-roadmap.md](路線圖-roadmap.md)。

---

**何時更新本文**：新增或改名分層、改變禁止匯入規則、`create_app` 的注入參數增減、新增主要外部依賴、請求生命週期（中介層順序、提交規則）改變時。更新後同步 [README.md](README.md) 的對照表並執行 `tests/test_design_docs.py`。
