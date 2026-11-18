# 設定、部署、日誌與營運

> 狀態：已實作（保存期限清除與自助刪除帳號由 C-4c、資料庫可量得到的營運告警由 C-4d 實作；排程設定、平台負責的三項告警、正式部署、入口層與備份演練由 C-5 實作） ｜ 最後更新：2026-11-18 ｜ 對應 change：admin-unlimited-coins（管理員不扣幣的驗收腳本與小幫手 `?admin=1`；沒有新的日誌事件、環境變數或遷移）、adult-only-signup（第 3 節：遷移 `0019`；第 8 節：缺口表第八項的更正與展示環境的部署及驗收紀錄）、aws-demo-environment（第 8 節：AWS 展示環境的拓撲、計畫步驟、執行器、拆除、資料庫憑證方案與未達 production 的缺口；實作與驗收皆已完成並於 2026-11-17 封存，`build_plan` 與 `deploy_plan` 已對真實 AWS 執行，`rollback_plan`、`teardown_plan` 尚未）、rpg-turn-revision（設定鍵 `rpg.free_regenerations`、`rpg.revision_*` 日誌事件、修訂驗收腳本）、rpg-notebook、rpg-turn-engine、game-wallet、rpg-character-creation、rpg-script-authoring、rpg-data-and-script、remove-player-password-auth、platform-foundation、member-auth、age-verification-birthdate、cs-chat-core、usage-limits、admin-console、openai-compatible-provider、privacy-lifecycle、ops-alerting、launch-readiness、staging-deployment、google-first-login ｜ 規範依據：AGENTS.md 第 9、11 節
> 注意（2026-11-03）：本文件已納入 change ① `google-first-login` 的營運變化（新排程指令 `purge-unconfirmed-accounts`、告警改名為「帳號鎖定激增」、期限清除納入鎖定計次）與 change ② `remove-player-password-auth`：`EMAIL_VERIFICATION_REQUIRED` 已淘汰（留著只會警告一次，部署計畫檔含有它會被拒絕）；寄信設定只供營運告警信與管理員的重設密碼信使用，玩家不收任何信；部署多一個資料遷移 `0011`；載入測試、冒煙驗收與容器驗收改用在目標環境內簽出的測試玩家 cookie（`scripts/mint_test_sessions.py`）。
> 注意（2026-11-03）：本文件已納入 change `rpg-data-and-script`：成人模式的環境變數改為 `ADULT_MODE_ENABLED` 與 `ADULT_MODE_MODELS`（舊的 `SEXUAL_CONTENT_LEVEL3_*` 已淘汰：忽略、啟動時各警告一次、部署計畫檔含有會被拒絕；staging 與 production 不再一律禁止，須明確設定才生效）、新的人工類指令 `seed-scripts`（部署之後執行）、`purge-expired-data` 新增 RPG 存檔與回合部分內容兩類，以及遷移 `0012`。

本文件說明設定如何依環境載入、容器如何建置與執行、migration 的部署步驟、日誌與事件的規則、維運指令，以及測試與驗收的分層。逐項的環境變數說明在 README 的「環境變數與機密」與 `.env.example`。

## 1. 環境與設定載入

```mermaid
flowchart TD
    Start["啟動"] --> Env{"APP_ENV 有設定？"}
    Env -->|"沒有或不合法"| Refuse["拒絕啟動（不預設為 development）"]
    Env -->|"development"| Dev["讀取 .env（若存在）<br/>開發工具頁開啟"]
    Env -->|"testing"| Test["不讀 .env，使用記憶體 SQLite<br/>開發工具頁開啟"]
    Env -->|"staging 或 production"| Prod["只讀環境變數<br/>必要機密缺少即啟動失敗（fail-fast）"]
    Prod --> Req["SECRET_KEY（至少 32 字元）、DATABASE_URL、GEMINI_API_KEY<br/>另須告警收件人（ALERT_EMAIL_TO）與完整的寄信設定"]
    Req --> Flags["Secure cookie 與 __Host- 前綴、禁用除錯、信任 ProxyFix<br/>不得使用 sa 與 TrustServerCertificate"]
```

| 環境 | 用途 | 與 production 的差異 |
|---|---|---|
| development | 本機開發（SQL Server `project`） | 讀 `.env`、可不設寄信與告警收件人、可信任自簽憑證、開發工具頁 |
| testing | 自動測試 | 不讀 `.env`、預設記憶體 SQLite、開發工具頁 |
| staging | 雲端預備環境 | 與 production 相同（唯一的例外是可設定 `GEMINI_BASE_URL` 指向假模型伺服器） |
| production | 正式（Google Cloud：Cloud Run、Cloud SQL for SQL Server、Secret Manager） | 告警收件人與寄信設定必填（營運告警與管理員的重設密碼信用）；玩家不收任何信 |

機密（Gemini 金鑰、`SECRET_KEY`、資料庫連線、OAuth、寄信）只由環境變數或平台機密功能注入，以 `Secret` 包裝（`repr` 與 `str` 一律遮罩），不得出現在日誌、錯誤訊息或前端回應。

### 開發用通道（ngrok）

要在本機測試 Google 登入或信件連結，需要一個對外的 https 網域。使用者目前以 ngrok 當臨時網域（本機的臨時網址，**不是**雲端預備環境的網域；雲端預備環境用 Cloud Run 預設網域）。通道後面的程式只有兩種用法，不要混用：

| 用法 | 環境 | 信任轉送標頭的方式 | HSTS | cookie |
|---|---|---|---|---|
| 日常開發經通道 | `development` | 設定 `TRUST_PROXY_HEADERS=true`（只有 development 可以設定） | **不送**（瀏覽器會把 HSTS 記在通道網域，牽連之後的本機開發） | 無 `__Host-` 前綴、非 Secure |
| 本機預演預備環境（C-5b） | `staging`（SQLite 檔案） | 本來就信任入口層，**不需要也不得設定** `TRUST_PROXY_HEADERS` | 送出 | `__Host-` 前綴與 Secure |

`TRUST_PROXY_HEADERS` 在 testing、staging、production 設定了（不論值）會啟動失敗，訊息只指出變數名稱；development 的值只接受 `true` 或 `false`。它讓 `ProxyFix` 生效（單層代理），使經通道到達的請求被視為 https 與通道的主機名稱，POST 才會通過 CSRF 的來源比對；Origin 與請求的主機仍必須一致，不信任的來源照樣被拒絕。

**日常開發經通道的步驟**

1. 在 ngrok 後台取得固定的免費網域（每個帳號一個），在本機啟動應用程式後開通道：`ngrok http --url=<你的固定網域> 5000`（連接埠依你的啟動方式）。
2. `.env` 設定 `TRUST_PROXY_HEADERS=true` 與 `BASE_URL=https://<你的固定網域>`（不含路徑；Google 登入的回呼網址與信件連結因此指向通道）。
3. 在**開發用**的 Google OAuth 用戶端把「已授權的重新導向 URI」加上 `https://<你的固定網域>/auth/google/callback`。通道網址若變動（沒有固定網域時每次都會變），授權重新導向 URI 與 `BASE_URL` 都要同步改，否則 Google 會回 `redirect_uri_mismatch`。
4. 第一次用瀏覽器開通道網址時，ngrok 免費方案會先顯示一頁警告，點過一次後該網域 7 天內不再出現；測試 Google 登入前先開一次首頁。程式端的請求（例如冒煙腳本）改帶 `ngrok-skip-browser-warning` 標頭略過它。

**暴露風險與存取控制**

- 通道會把本機的開發環境**暴露在網際網路上**：知道網址的任何人都能連到開發工具頁（只在 development 與 testing 註冊的元件示範頁）與你建立的測試帳號。
- 建議在通道端加上存取控制（例如 ngrok 的基本驗證；免費方案是否提供以 ngrok 當時的方案為準）。沒有的話，只在需要測試的時段開著通道，其餘時間關閉；測試帳號一律用保留的 `.invalid` 信箱與可丟棄的資料，不放真實個資。
- 開發用的 OAuth 用戶端**不用於預備或正式環境**（AGENTS.md 第 10 節）；預備環境另建專用的用戶端，授權重新導向 URI 只放預備環境的回呼網址。
- 驗證：`tests/test_development_tunnel.py`（經通道的 POST 通過 CSRF、不設定時維持拒絕、其他環境拒絕、值不合法、development 經通道仍沒有 HSTS、回呼網址取自 `BASE_URL`、本小節載明風險）。

## 2. 容器與部署拓樸

```mermaid
flowchart LR
    Dev["開發者與 CI"] -->|"docker build"| Image["映像<br/>python:3.14-slim-bookworm<br/>含 msodbcsql18 與前端資產<br/>非 root（uid 10001）、不含 .env"]
    Image --> Registry["映像庫"]
    Registry --> Run["Cloud Run 執行個體<br/>gunicorn gthread"]
    Sm["Secret Manager"] -->|"注入環境變數"| Run
    Run --> Sql[("Cloud SQL for SQL Server")]
    Run --> Gem["Gemini API"]
    Run --> Mail["SMTP"]
    Ingress["入口層（C-5）<br/>HTTPS、HTTP/2、HSTS、IP 限流"] --> Run
    Job["平台排程器"] -->|"flask 指令"| Run
```

| 項目 | 設定 |
|---|---|
| 執行 | `gunicorn --config gunicorn.conf.py wsgi:app`；`gthread`、`WEB_CONCURRENCY`（預設 2）個 worker、`GUNICORN_THREADS`（預設 8）、`timeout` 60 秒、`graceful_timeout` 30 秒 |
| 連接埠 | 取自 `PORT`（預設 8080）；映像有 `HEALTHCHECK`（`/healthz`） |
| RPG 串流池 | `gunicorn --config gunicorn.rpg.conf.py wsgi:app`（映像兩份設定都有，預設啟動客服池）：`gthread`、`WEB_CONCURRENCY`（預設 2）、`GUNICORN_THREADS`（預設 16：每個進行中的串流佔一條請求執行緒，外加一條上游讀取執行緒）、`timeout` 180 秒（大於單次串流生成上限 60 秒與心跳間隔 15 秒，也涵蓋一個回合最壞約 150 秒）、`graceful_timeout` 60 秒（讓進行中的回合寫回：完成、結算或退款）。**記事簿抓取（rpg-notebook）在回應關閉之後於同一條請求執行緒上執行**，每完成 10 輪最多再佔用該執行緒約 60 秒（串流本身已在 `done` 之後結束，玩家不等待；抓取總預算 60 秒，小於 `timeout` 180 秒）；RC-4 估算執行緒時要計入。初始值，RC-4 壓測校正。**`/api/game/` 分流到這個池**：AWS 展示環境已由入口（Caddy 的路徑規則）實現並驗證（2026-11-17，見第 8 節「驗證分流」）；Google Cloud 的 production 仍屬後續的部署工作，在那之前與客服共用同一個池 |
| 禁止 | 不得用 `app.run()`（開發伺服器）執行正式環境；客服與 RPG 串流使用獨立的 worker 池（設定檔已分開；路徑分流在 AWS 展示環境已驗證，Google Cloud 的 production 待部署） |
| 驗收 | `scripts/verify_container.py`（經 WSL 的 Docker）：建置、非 root、無 `.env`、各環境啟動與健康檢查、缺少機密時只列變數名稱、會員與客服的 HTTP 流程、保存期限指令 |

### 傳輸安全與安全標頭（C-5a）

入口層（Cloud Run）負責 HTTPS 與 HTTP/2，但**不會替我們加任何安全標頭**，所以由應用程式自己送（`app/security/security_headers.py`，在每個回應上，含錯誤與靜態資源；端點已自行設定同名標頭時不覆蓋）：

| 標頭 | 值 | 環境 |
|---|---|---|
| `Strict-Transport-Security` | `max-age=31536000`（一年，**不含** `includeSubDomains` 與 `preload`） | 只有 staging 與 production（有 HTTPS 入口層的環境；development 即使經通道、信任了轉送標頭也不送） |
| `X-Content-Type-Options` | `nosniff` | 全部 |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | 全部 |
| `X-Frame-Options` | `DENY` | 全部 |
| `Permissions-Policy` | 停用相機、麥克風、定位、付款、USB | 全部 |

- **development 與 testing 不送 HSTS**：本機是 http，瀏覽器會把 HSTS 記在 `localhost`，之後的本機開發全部失敗。development 經 HTTPS 通道使用（`TRUST_PROXY_HEADERS=true`，見「開發用通道（ngrok）」）時同樣不送：判斷依據是環境（`is_production_like`），不是「是否信任轉送標頭」。
- **HSTS 為何只設一年且不含子網域與預載入**：瀏覽器記住後無法由伺服器撤回，網域與其他子網域尚未確定；日後要升級是放寬（延長時間、加子網域），而不是撤回。
- **刻意不送 `Content-Security-Policy`（已知限制）**：前端使用 Vue 全域建置，模板在瀏覽器以 `new Function` 編譯，政策必須允許 `unsafe-eval`（加上 `style=` 屬性還要 `unsafe-inline`），這樣的 CSP 幾乎沒有保護力。替代做法是預先編譯模板，等於改變前端建置，違反「不使用打包」的規範，不在 C-5a 範圍；前端改版時重新評估。這項與入口層 IP 限流一起列在「未滿足的規範項目」。
- 驗證：`tests/test_security_headers.py`（五種路徑、錯誤回應、四個環境、自訂標頭不被覆蓋、沒有 CSP）。

> 注意（2026-11-04）：本文件已納入 change `rpg-script-authoring`（RC-7）：遷移 `0013`（部署順序不變：備份 → 遷移 → 部署 → `flask seed-scripts`；七個營運限制的初始值由遷移寫入，不必另外執行指令）、兩個人工類指令 `set-script-limit` 與 `show-script-limits`（限制存在資料庫、立即生效、每次修改寫 `limit.set` 稽核）、`purge-expired-data` 新增劇本送審紀錄一類（365 天）、劇本事件的日誌白名單，以及容器驗收新增的七項檢查。
> 注意（2026-11-10）：本文件已納入 change `rpg-turn-engine`（RC-2a）：遷移 `0016`（部署順序不變：備份 → 遷移 → 部署；只新增欄位並補一列 `rpg.max_months_per_turn`）、RPG 串流池的獨立 gunicorn 設定 `gunicorn.rpg.conf.py`（映像兩份設定都有，路徑分流待部署；2026-11-17 更新：AWS 展示環境已分流並驗證，Google Cloud 的 production 仍待部署，見第 8 節）、人工類指令 `set-rpg-setting`、`show-rpg-settings`，以及 `rpg.*` 日誌事件與六個新的日誌欄位。
> 注意（2026-11-09）：本文件已納入 change `game-wallet`（W1）：遷移 `0015`（部署順序不變：備份 → 遷移 → 部署；只新增 `wallets`、`wallet_entries` 兩張表並補五個遊戲幣設定鍵，已有的鍵不蓋掉）、兩個人工類指令 `set-wallet-setting`、`show-wallet-settings`（部署後以 `show-wallet-settings` 確認五個值）、遊戲幣事件 `wallet.*` 與三個新的日誌欄位 `coins`、`lost_coins`、`balance`。
> 注意（2026-11-04）：本文件已納入 change `rpg-character-creation`（RC-9）：遷移 `0014`（部署順序不變：備份 → 遷移 → 部署；只新增 `sessions.protagonist_json` 並補三個每日主角生成次數的限制鍵，已有的鍵不蓋掉）、`show-script-limits` 與 `set-script-limit` 現在涵蓋十個鍵、主角事件的日誌白名單，以及開存檔最壞約 60 秒（劇本尚未審查過時再加一次審查；前端 axios 逾時請設 65 秒）。

## 3. Migration 的部署步驟

```mermaid
sequenceDiagram
    participant Op as 部署者
    participant Db as 資料庫
    participant Old as 舊版程式
    participant New as 新版程式

    Op->>Db: 備份（COPY_ONLY + CHECKSUM）並 RESTORE VERIFYONLY
    Op->>Db: alembic upgrade head（獨立步驟，應用程式啟動時不會自動執行）
    Note over Old,New: expand-only：新增欄位先可為空，新舊程式都能在這個結構上運作
    Op->>New: 部署新版程式
    Op->>New: flask seed-scripts（人工類：建立或更新內建劇本，冪等；有新的內建劇本資料檔時才有變動）
    Op->>Db: 之後的 change 才回填資料並加上約束（contract）
```

- `0019_adult_declaration`（adult-only-signup）只新增兩個 `users` 欄位（`adult_declared_at`、`adult_declaration_version`：皆可為空、不回填、不建索引；downgrade 以 `recreate="never"` 直接移除），是 expand-only：舊版程式在新結構上照常運作（不讀這兩欄）。部署順序不變：備份（展示環境為手動快照）→ migration → 新版程式；`is_adult_at_confirmation` 不再寫入（欄位保留，日後的 contract 遷移再移除）。**狀態**：程式碼在 0019；**展示環境 RDS 已於 2026-11-18 由 0018 升到 0019**（映像 `87ebcd8`，部署與驗收紀錄見第 8 節「adult-only-signup 的部署與驗收」）；本機 `project` 仍在 0017；`rpg-ops-loadtest` 若需要遷移，編號順延為 `0020`。
- `0013_rpg_script_authoring` 的 `runtime_limits` 的初始值由遷移以 ORM 資料表寫入（`0013` 七個，`0014` 再補三個每日主角生成次數，只插入資料庫還沒有的鍵）；之後調整只用 `flask set-script-limit`（不必重新部署，程式每次使用時現讀）。資料缺值或不合法時（例如某一列被誤刪）讀取端用保守的保底預設並記一筆警告（只記鍵名）；表本身仍須先由遷移建立，所以順序不能反過來。

### 部署與回復（C-5a）

部署用 `scripts/gcp_launch.py`（Python 加 `gcloud`，符合第 8 節「維運腳本用 Python」）。**預設只印出將執行的指令**，加 `--execute` 才逐步執行，而且**每一步執行前都要確認**；專案、區域、環境與資料庫執行個體必須明確提供（沒有預設的專案）。步驟的內容與順序在 `scripts/launch_plan.py`（純函式，有測試，不需要 `gcloud`）。

| 子命令 | 做什麼 |
|---|---|
| `plan` | 預覽或執行以下全部，順序固定 |
| `secrets` | 啟用 API、建立執行用的服務帳號、建立 Secret Manager 的機密容器並寫入值（值從標準輸入讀取，不經命令列）、授予讀取權限 |
| `build` | 建立映像庫、以 Cloud Build 建置並推送映像（內容依 `Dockerfile` 與 `.dockerignore`，不含 `.env`） |
| `migrate` | **先備份**（隨需備份 Cloud SQL），再以 Cloud Run Job 執行 `alembic upgrade head`；失敗不自動重試，**失敗即停止，不部署新版程式** |
| `deploy` | 部署服務，再驗證 `/healthz`（存活）與 `/readyz`（含資料庫）都回 200 |
| `schedule` | 為四個排程指令（見「維運指令與保存期限」）各建一個 Cloud Run Job 與 Cloud Scheduler |
| `alerts` | 平台告警定義（見「告警條件一覽」） |

```
python scripts/gcp_launch.py plan --project <專案> --region asia-east1 --env staging     --sql-instance cs-sql-staging --image-tag <標籤> --vars-file staging.vars
```

- **設定的歸類**：`.env.example` 的每個變數必須恰好是「機密」（Secret Manager，`--set-secrets` 只有名稱與版本參照）、「一般設定」（環境變數）或「不部署」（`TEST_SQLSERVER_URL`、`FLASK_DEBUG`）之一，新增變數而忘了歸類會讓測試失敗。一般設定放在 `--vars-file`（KEY=VALUE，不進版本庫，已列入 `.gitignore`）；**含機密名稱的檔案會被拒絕**。`APP_ENV` 由計畫依環境填入。
- **環境隔離**：所有資源名稱帶環境名稱（`cs-web-staging`、`cs-staging-secret-key`…），`--sql-instance` 必須含環境名稱，staging 與 production 沒有共用的資源、資料庫或機密。計畫**不建立 Cloud SQL 執行個體**（費用由你決定，N-9），只對既有的執行個體備份。
- **連線資料庫**：Cloud SQL for SQL Server 沒有 Cloud Run 的 unix socket 連線，要走私有 IP（Direct VPC egress，`--network` 與 `--subnet`），`DATABASE_URL` 指向私有 IP。這一段尚未對真實環境驗證（C-5b）。
- **新版本的部署順序**：`build` → `migrate`（含備份）→ `deploy`（含驗證）→ 需要時 `schedule`。migration 是 expand-only，新舊版程式都能在新結構上運作（見第 3 節）。
- **回復**：用前一個映像標籤重新執行 `deploy`（`--image-tag <前一版>`），**不需要回復資料庫結構**。若 `migrate` 失敗：停止，不部署；資料庫仍是舊結構，舊版程式照常運作；必要時依「資料庫還原」從備份還原。
- **限制（誠實揭露）**：這份腳本只在預覽輸出與測試中驗證過，**沒有對真實的 `gcloud` 與雲端專案執行過**；`gcloud` 的旗標會隨版本變動，第一次實際執行在 C-5b 的預備環境，發現的差異回寫到 `scripts/launch_plan.py`。

### 資料庫還原與重新抹除（C-5a）

使用者刪除帳號之後，資料庫從備份還原會讓他**復活**（個資法的刪除權因此失效）。規範要求還原時依 `deletion_tombstones` 重新執行刪除；備份**之後**才發生的刪除不在還原後的資料庫裡，所以另外從日誌補上。順序固定，**重新抹除必須在還原之後、對外開放流量之前**：

```mermaid
sequenceDiagram
    participant Op as 維運者
    participant Db as 還原後的資料庫
    participant Log as 日誌（保存 30 天）
    participant Svc as 服務

    Op->>Svc: 先關閉流量（還原期間不接受請求）
    Op->>Db: 還原資料庫（點對點或備份）
    Op->>Db: 結構檢查（alembic current 與預期版本相同）
    Op->>Log: 取出 account.deleted_by_request 與 account.inactive_purged 的「編號,時間」
    Op->>Db: flask replay-deletions --dry-run --ids-file 檔案（先看筆數）
    Op->>Db: flask replay-deletions --ids-file 檔案（實際抹除）
    Op->>Db: 驗證（已刪除的帳號不存在、/readyz 回 200）
    Op->>Svc: 開放流量
```

| 步驟 | 指令 | 說明 |
|---|---|---|
| 取出日誌 | `python scripts/restore_drill.py logs-to-ids --input 日誌.json --output 刪除.txt` | 只取內部編號與時間，**丟棄其餘全部欄位**；日誌可由 `gcloud logging read` 匯出（過濾 `jsonPayload.event` 為上述兩個事件）。日誌保存 30 天，與備份相同，所以涵蓋範圍一致 |
| 預覽 | `flask --app wsgi set-wallet-setting KEY VALUE` | **人工類，不得排程**：修改一個遊戲幣設定（`coin.turn_cost.standard` 每輪幣價 30、`coin.signup_grant` 首次註冊贈幣 200、`coin.checkin_grant` 每日簽到 200、`coin.free_cap` 免費幣上限 1000、`coin.hold_timeout_seconds` 預扣逾時秒數 180）；驗證鍵與範圍後寫入，輸出舊值與新值並寫一筆 `limit.set` 稽核（管理員欄位為空）。立即生效，不必重新部署；只接受遊戲幣組的鍵（用錯會指出該用哪個指令） |
| `flask --app wsgi show-wallet-settings` | **人工類，不得排程**：列出五個鍵、目前生效的值與範圍；資料庫缺值或不合法的鍵標示「保底預設」。輸出只有鍵與數值 |
| `flask --app wsgi set-rpg-setting KEY VALUE` | **人工類，不得排程**（rpg-turn-engine）：修改一個 RPG 回合引擎設定（`rpg.max_months_per_turn`：單一回合最多推進幾個遊戲內月份，預設 24，範圍 0 至 240；`rpg.free_regenerations`〔rpg-turn-revision〕：每個回合前幾次修訂免費，**重新生成與編輯共用同一組次數**，成功才計次、編輯不歸零，預設 2，範圍 0 至 20，第 N+1 次起每次消耗一輪的幣價）；驗證鍵與範圍後寫入，輸出舊值與新值並寫一筆 `limit.set` 稽核（管理員欄位為空）。立即生效，不必重新部署；只接受 RPG 組的鍵（用錯會指出該用哪個指令） |
| `flask --app wsgi show-rpg-settings` | **人工類，不得排程**：列出 RPG 組的鍵、目前生效的值與範圍；資料庫缺值或不合法的鍵標示「保底預設」。輸出只有鍵與數值 |
| `flask --app wsgi replay-deletions --dry-run [--ids-file 刪除.txt]` | 只輸出筆數：將抹除、略過（編號已被重新發出）、略過（管理員）、已不存在。**「略過（編號已被重新發出）」的數字要人工看過** |
| 實際抹除 | `flask --app wsgi replay-deletions [--ids-file 刪除.txt]` | 與一般抹除走同一個出口（清帳本的對話編號 → 條件式刪除 → 提交），可重複執行；抹除成功時補寫一筆刪除紀錄（原因 `restore_replay`、時間沿用原本的刪除時間），之後再還原仍涵蓋 |

- **資料來源的實情**：還原後資料庫裡的 `deletion_tombstones` 只涵蓋**備份之前**的刪除，而那些帳號本來就不在備份裡；真正會復活的是**備份之後**才刪除的帳號，它們的紀錄只在現場資料庫（如果還能存取就先匯出）與日誌裡。所以日誌取出的檔案才是主要來源，資料庫內的紀錄是補充（例如還原到另一個資料庫、仍保有現場紀錄的情況）。日誌保存 30 天、備份也保存 30 天，所以任何可還原的備份都被日誌涵蓋。
- **為什麼要比對建立時間**：SQL Server 的 IDENTITY 在還原後從備份時的種子繼續，備份之後註冊再被刪除的帳號，其編號會被還原後的新使用者沿用；只憑編號刪除會刪到別人。指令只抹除「建立時間不晚於刪除時間」且不是管理員的帳號，條件在刪除當下由資料庫判斷（條件式 DELETE）。同一個編號有多筆紀錄時只看最晚的刪除時間。
- **檔案先整體驗證**：任何一行格式錯誤，指令在抹除任何帳號之前失敗，只指出行號，不回顯該行內容與檔案路徑。檔案以 UTF-8（可帶 BOM）讀取。
- **日誌與輸出**：事件 `account.deletion_replayed` 只有內部編號；輸出只有筆數；預覽不記事件。
- **演練**：`scripts/restore_drill.py`（把備份還原到名稱帶 `_restore_drill` 的暫存資料庫，依序預覽與實際抹除並檢查已刪除者不存在）。演練的結果記在 README「驗收紀錄」；**每季演練一次**（AGENTS.md 第 11 節），C-5b 在預備環境實際演練。

## 4. 日誌與事件

```mermaid
flowchart LR
    Req["請求日誌<br/>request_id、路徑規則（不含查詢字串）、方法、狀態碼、延遲"] --> Fmt
    Sec["安全事件<br/>auth.*、security.*、account.*"] --> Fmt
    Llm["模型事件<br/>llm.call、llm.breaker_opened、cs.reply"] --> Fmt
    Use["用量事件<br/>usage.denied、usage.global_budget_warning、cs.chat_suspended"] --> Fmt
    Adm["後台事件<br/>admin.access_denied、admin.reauth_failed、admin.action"] --> Fmt
    Lif["生命週期事件<br/>account.deletion_requested、account.deletion_cancelled、account.deleted_by_request、account.exported、account.deletion_replayed、retention.purged、retention.purge_failed"] --> Fmt
    Scr["劇本事件<br/>account.nickname_set、script.*、limits.*"] --> Fmt
    Pro["主角事件<br/>protagonist.*、session.started"] --> Fmt
    Wal["遊戲幣事件<br/>wallet.*"] --> Fmt
    Rpg["RPG 回合事件<br/>rpg.*"] --> Fmt
    Alr["告警事件<br/>alert.firing、alert.resolved、alert.delivery_failed、alert.check_failed、alert.check_completed"] --> Fmt
    Fmt["JsonFormatter<br/>白名單欄位 + 訊息遮罩 + 例外只留類型與位置"] --> Out["標準輸出（單行 JSON）"]
```

- **白名單**：只輸出固定的基本欄位與 `ALLOWED_FIELDS`（`request_id`、`path`、`method`、`status`、`latency_ms`、`user_id`、`conversation_id`、`intent`、`model`、`attempt`、`prompt_tokens`、`completion_tokens`、`finish_reason`、`error_type`、`error_location`、`event`，以及模型呼叫的 `kind`、`provider`、`outcome`、`thinking_tokens`、`reason`、`provider_status`，以及用量事件的整數 `used`、`limit`，與期限清除、營運告警的整數 `count`，以及營運告警的固定列舉 `severity`），其餘一律丟棄。
- **絕不記錄**：密碼與雜湊、cookie、CSRF 與驗證／重設權杖、OAuth code、Authorization 標頭、完整金鑰、email、生日與年齡區間、使用者訊息、`reply_text`、模型原始輸出、含權杖的網址。事件名稱是固定列舉，失敗原因寫在名稱最後一段，所以日誌裡不可能夾帶自由文字。
- 模型呼叫的函式（`app/observability/llm_events.py`）根本沒有接收文字內容的參數；`tests/test_log_scan_full_flow.py` 走完整個流程後掃描整段日誌，找不到任何測試用的敏感字串。
- 用量事件（`app/observability/usage_events.py`）同樣只有分類與整數，原因是固定列舉（拒絕：`rate_limited`、`daily_quota`、`conversation_limit`、`suspended`、`global_budget`；預算警告：`tokens`、`calls`；暫停：`safety_blocks`），傳入列舉以外的值會立刻出錯。`usage.global_budget_warning` 在全站今日的 token 或呼叫次數越過上限的 80% 後，每個請求都記一筆（沒有跨 worker 的共享狀態可「每日只記一次」），由告警層（C-4d 的 `check-alerts`）以狀態去重；`cs.chat_suspended` 是日誌事件；後台總覽的「暫停中的使用者數」與被標記清單的暫停截止時間不靠它，而是由帳本以與 `UsageService` 相同的純函式（`quota_policy.suspension_until`）推得。
- 後台事件（`app/observability/admin_events.py`）：`admin.access_denied`（非管理員碰到後台，只有內部編號）、`admin.reauth_failed`（重新輸入密碼失敗）、`admin.action`（停權、解除、調整等級、刪除對話成功）。欄位只有 `event`、`user_id`（操作者的內部編號；`admin.access_denied` 時是被擋下的非管理員）與 `reason`（固定列舉：動作名稱或 `not_admin`），傳入列舉以外的值立刻出錯；**沒有** email、訊息、管理員填寫的原因與目標使用者，`tests/test_admin_flow.py` 走完整段流程後掃描日誌證明這一點。稽核的完整內容在資料庫的 `admin_audit_logs`，不在日誌。
- 生命週期事件（`app/observability/lifecycle_events.py`）：申請刪除、撤銷、被抹除（寬限期滿，原因 `user_requested`）、匯出（`reason` 為 `json` 或 `csv`）、期限清除某一類完成（`reason` 為類別、`count` 為筆數）或失敗（只有類別，不記錄例外內容）。欄位只有內部編號、固定列舉與整數；列舉以外的值立刻出錯。閒置帳號清除沿用既有的 `account.inactive_purged`。`tests/test_privacy_lifecycle_flow.py` 走完整段流程（註冊、對話、匯出、申請、撤銷、再申請、排程刪除、一年多之後的期限清除）後掃描日誌與指令輸出，找不到 email、名稱、生日、密碼、確認文字、對話與失敗原文。
- 劇本事件（`app/observability/script_events.py`）：暱稱設定（`account.nickname_set`）、劇本建立／更新／新版本／刪除／撤回／重新取用筆名、送審（`script.submitted`）、自動拒絕（`script.auto_rejected`）、模型層審查（`script.review`）、審查不可用（`script.review_unavailable`）、暫停與解除暫停發佈（`script.publish_suspended`、`script.publish_lifted`），以及營運限制缺值或不合法與被修改（`limits.invalid`、`limits.changed`）。欄位只有內部編號、固定列舉（階段 `kind`、層級 `outcome`、類別 `reason`）與整數（字數 `count`、嘗試次數 `attempt`）、模型名稱；函式簽名沒有任何文字參數，類別與原因傳入列舉以外的值立刻出錯。絕不出現暱稱、短代號、筆名、劇本的任何文字、標題、標籤、申訴內容與模型原文；`tests/test_script_events.py` 守住簽名與列舉，`tests/test_log_scan_script_authoring.py` 走完整個創作與送審流程後掃描日誌。
- 主角事件（`app/observability/protagonist_events.py`）：生成預覽（`protagonist.suggested`，帶今日剩餘次數 `count`）、生成不可用（`protagonist.suggest_unavailable`）、生成次數用盡（`protagonist.suggest_limited`，帶 `used`／`limit`）、被自動拒絕（`protagonist.rejected`，階段 `kind` 為 `suggest`／`start`、層級 `outcome` 為 `rules`／`model`、類別 `reason`）、模型層審查（`protagonist.review`）與不可用（`protagonist.review_unavailable`），以及開出存檔（`session.started`，內容模式 `kind`、是否帶錨定事實 `outcome`）。欄位只有內部編號、固定列舉與整數、模型名稱；函式簽名沒有任何文字參數，列舉以外的值立刻出錯。絕不出現自我介紹、角色卡、錨定事實與模型原文；`tests/test_protagonist_events.py` 守住簽名與列舉，`tests/test_log_scan_protagonist.py` 走完整個主角流程後掃描日誌。
- 遊戲幣事件（`app/observability/wallet_events.py`）：`wallet.signup_granted`、`wallet.checkin`、`wallet.checkin_overflow`、`wallet.hold`、`wallet.settle`、`wallet.refund`、`wallet.stale_released`、`wallet.insufficient`、`wallet.adjust`、`wallet.grant_deferred`；欄位只有內部使用者編號、整數的幣量 `coins`、作廢量 `lost_coins`、餘額 `balance`，以及固定列舉的餘額種類（`kind`：`free`、`paid`）與模型鍵（`model`）。函式沒有接收冪等鍵（帶有存檔識別碼與前端產生的訊息編號）、存檔識別碼、管理員理由或任何玩家文字的參數，傳入列舉以外的值或不是整數的幣量立刻出錯（`tests/test_wallet_events.py` 與 `tests/test_wallet_flow.py` 的日誌掃描守住）。
- RPG 回合事件（`app/observability/rpg_events.py`）：`rpg.turn_started`（`start` 為 `new` 或 `restarted`）、`rpg.turn_completed`（字數、延遲、首字毫秒、模型、結束原因、狀態呼叫丟棄的項目數）、`rpg.turn_blocked`（被擋的類別列舉與是否為第二次）、`rpg.turn_interrupted`（原因：`disconnected`、`timeout`、`model_failed`）、`rpg.turn_failed`、`rpg.state_failed`、`rpg.session_ended`（原因 `age` 或 `proposed`）、`rpg.opening_started`／`ready`／`fallback`／`reset`／`taken_over`；記事簿事件 `rpg.notebook_captured`（追加成功：邊界、條數、最長一條、追加與總字數、版本，以及超出提示詞要求的條數與字數）、`rpg.notebook_skipped`（原因 `full`、`empty`、`budget`）、`rpg.notebook_abandoned`（原因 `blocked`、`too_long`、`stale`，被擋時附類別列舉）、`rpg.notebook_failed`（原因 `transient`、`timeout`、`config`、`unavailable`、`validation`、`blocked`；標記不推進，由後續回合補做）、`rpg.notebook_saved`、`rpg.notebook_save_rejected`（原因 `too_long`、`blocked`、`conflict`、`ended`）；修訂事件（rpg-turn-revision，重新生成與編輯最後一輪）`rpg.revision_started`（種類 `kind` 為 `regenerate` 或 `edit`、認領後的 `attempt`、是否免費〔`outcome` 為 `free`／`paid`，兩種操作依同一組免費次數決定〕、預扣的幣數 `coins`，免費為 0）、`rpg.revision_completed`（種類、`attempt`、字數 `chars`、延遲、世界狀態是否有變更〔`reason` 為 `world_updated`／`world_unchanged`〕、存檔是否因此完結〔`outcome` 為 `ended`／`complete`〕）、`rpg.revision_failed`（種類、`attempt`、原因 `blocked`、`timeout`、`model_failed`、`state_failed`、`superseded`、`disconnected`、`internal`；保留舊內容、已退款或釋放認領）。函式沒有接收文字的參數，列舉外的值立刻 `ValueError`；欄位只有內部編號（`user_id`、`session_id`）、整數（`turn_number`、`attempt`、`chars`、`first_chunk_ms`、`dropped`）與列舉（`categories`）；`tests/test_rpg_flow.py` 走完整段旅程後掃描日誌，找不到劇情、行動、NPC 名稱、被擋文字或標記字串。告警：被擋比率與串流失敗率尚未列入告警規則（RC-4 量測後決定）。
- 告警事件（`app/observability/alert_events.py`）：`alert.firing`（進入、升級或提醒；`reason` 為告警代碼、`severity` 為 `warning` 或 `critical`、`count` 為整數數值）、`alert.resolved`、`alert.delivery_failed`（這一次通知沒有寄出，只有告警代碼）、`alert.check_failed`（這一項評估失敗，只有告警代碼）、`alert.check_completed`（**心跳**：`check-alerts` 完整跑完一次，只有事件名稱；預覽、唯讀與有評估失敗的執行不記。寄信失敗那次仍記，因為檢查本身跑完了；平台用它偵測「告警檢查停擺」，見「平台告警」）。欄位只有固定列舉與整數，**沒有收件人地址、信件內容、使用者資料與例外訊息**；列舉以外的值立刻出錯；預覽不記事件。`tests/test_alerting_flow.py` 以真正的客服對話與登入走完整條生命週期後掃描日誌、指令輸出與信件。
- 層級：設定錯誤（400、403）為錯誤層級；驗證失敗與暫時性錯誤為警告；主要模型因逾時或伺服器錯誤將被暫時跳過時，在導致斷路的那次失敗記一筆 `llm.breaker_opened`。
- 健康檢查：`/healthz`（存活，不碰資料庫）與 `/readyz`（確認資料庫），回應不含內部細節且 `no-store`。

## 5. 維運指令與保存期限

指令分兩類，清單在 `app/cli.py`（`SCHEDULED_COMMANDS`、`MANUAL_COMMANDS`）並由 `tests/test_scheduled_commands.py` 守住：**排程類**由平台排程器觸發（`check-alerts` 每 5 分鐘 `*/5 * * * *`；`purge-unconfirmed-accounts` 每小時整點 `0 * * * *`（24 小時的年齡確認期限）；`purge-inactive-accounts` 03:00、`execute-account-deletions` 03:20、`purge-expired-data` 03:40，每日，Asia/Taipei，錯開以免同時佔用資料庫連線）；**人工類**（`create-admin`、`age-stats`、`llm-probe`，以及 C-5a 的 `replay-deletions`、`rpg-data-and-script` 的 `seed-scripts`）不得排程。新增指令必須歸入其中一類，否則測試失敗；部署計畫的排程清單從同一份清單產生。

| 指令 | 用途 |
|---|---|
| `flask --app wsgi create-admin` | 一次性建立首位管理員（密碼只能互動輸入或從標準輸入讀取，不接受命令列參數） |
| `flask --app wsgi purge-unconfirmed-accounts [--dry-run]` | 刪除玩家首次以 Google 登入後 24 小時內沒有完成年齡確認的帳號（管理員、已完成者與沒有期限記錄的舊帳號不納入；每小時），原因記為 `age_unconfirmed`；與閒置清除共用同一個抹除出口 |
| `flask --app wsgi purge-inactive-accounts [--dry-run]` | 刪除連續 12 個月未登入的帳號（管理員不納入），每個帳號在同一交易寫入 `deletion_tombstones` |
| `flask --app wsgi execute-account-deletions [--dry-run]` | 抹除「使用者申請刪除、寬限期（7 天）已滿」的帳號，原因記為 `user_requested`；與閒置清除共用同一個抹除出口（先清帳本的對話編號、再條件式刪除、寫刪除紀錄，條件不成立整個回復）。可重複執行，輸出只有筆數 |
| `flask --app wsgi purge-expired-data [--dry-run]` | 清除超過保存期限的資料：對話與訊息（365 天）、帳號鎖定計次（7 天）、失敗的模型原文（14 天，清為空值）、用量帳本（365 天）、驗證權杖（到期後 7 天）、管理稽核（730 天）、RPG 存檔（最後活動後 365 天，回合與記事簿一併刪除）、劇本送審紀錄（365 天，只刪紀錄，暫停旗標不受影響）、中斷或被擋回合的部分劇情內容（14 天，清為空字串、回合紀錄保留）。各類獨立、分批（500）、每批各自提交；某一類失敗不阻止其他類別，但指令以非零代碼結束並標明類別。輸出與日誌只有各類筆數 |
| `flask --app wsgi check-alerts [--dry-run]` | 評估五項只靠資料庫量得到的營運告警（見下方「告警條件一覽」），狀態有變化時寄電子郵件給 `ALERT_EMAIL_TO`（進入、升級、每 6 小時提醒、恢復各一封，不隨檢查頻率重複寄）。`--dry-run` 與沒有設定收件人時只評估並顯示結果，不寄信也不改變狀態。輸出只有告警名稱、狀態與數值；寄信失敗或某項評估失敗時以非零代碼結束 |
| `flask --app wsgi age-stats` | 各年齡區間的人數，沒有任何個別使用者的資料 |
| `flask --app wsgi seed-scripts [--dry-run]` | **人工類，不得排程，不在應用程式啟動時執行**：部署（備份、遷移、部署新版程式）之後執行，讀取隨映像封裝的 `app/data/scripts/*.json`，全部驗證通過才在單一交易內寫入（全有或全無），以種子鍵與標準化內容的雜湊冪等地建立、更新（新增版本並取代舊版）或略過；資料不合規時以非零代碼結束並指出檔名與欄位路徑，沒有寫入任何資料；輸出只有種子鍵與筆數。雲端的部署計畫腳本尚未為它產生一次性的 Job（屬 C-5c），目前由部署者以同一個映像執行 |
| `flask --app wsgi set-script-limit KEY VALUE` | **人工類，不得排程**：修改一個營運限制（每位作者可擁有的劇本數 `script_limit.<等級>`、每個台灣日的送審次數 `script_submit_daily.<等級>`、每個台灣日請 AI 生成主角的次數 `protagonist_suggest_daily.<等級>`、連續被自動拒絕幾次就暫停發佈 `script_reject_streak`）。驗證鍵與範圍後寫入，輸出舊值與新值，寫一筆 `limit.set` 稽核（管理員欄位為空）；立即生效，不必重新部署。沒有任何 HTTP 端點可以修改限制 |
| `flask --app wsgi show-script-limits` | **人工類，不得排程**：列出全部十個鍵、目前生效的值與範圍；資料庫缺值或不合法的鍵標示「保底預設」。輸出只有鍵與數值 |
| `flask --app wsgi replay-deletions [--dry-run] [--ids-file 檔案]` | **人工類，不得排程**：備份還原後、對外開放流量之前，依 `deletion_tombstones`（與日誌取出的「編號,時間」檔案）重新抹除復活的帳號；只抹除建立時間不晚於刪除時間且非管理員的帳號；輸出只有筆數。步驟見「資料庫還原與重新抹除」 |
| `flask --app wsgi llm-probe [--text 短文字] [--structured]` | **開發用**（不是維運工作，不由排程器觸發）：呼叫 RPG 敘事用途的端點一次（非串流），印出回覆、模型、目前的推理設定（`OPENROUTER_REASONING`）、延遲、token 與結束原因；主控台編碼（如 cp950）無法表示的字元以替代字元顯示，不會中斷；`--structured` 評估 JSON 穩定度。只在 development 與 testing 執行，staging 與 production 拒絕；不寫帳本與任何資料表，不顯示金鑰 |

| 資料 | 保存期限 | 狀態 |
|---|---|---|
| 帳號（含生日與年齡區間） | 連續 12 個月未登入即刪除；使用者申請刪除後 7 天；玩家首次以 Google 登入後 24 小時內沒有完成年齡確認即刪除 | 已實作（`purge-inactive-accounts`、`execute-account-deletions`、`purge-unconfirmed-accounts`） |
| 對話與訊息 | 最後一則訊息後 12 個月，或使用者主動刪除 | 已實作（`purge-expired-data`，最後活動早於 365 天） |
| RPG 存檔（連同回合與記事簿） | 最後活動後 12 個月（沒有回合的存檔以建立時間算）；帳號刪除時一併刪除 | 已實作（`purge-expired-data`，365 天，不論進行中或已完結）；隱私權政策的保存期限表尚未補上這一列（留給法務覆核） |
| 中斷或被擋回合的部分劇情內容 | 14 天 | 已實作（`purge-expired-data` 清為空字串，回合紀錄保留） |
| 劇本送審紀錄（`script_submissions`，只有結果、類別與時間） | 12 個月；帳號刪除時一併刪除 | 已實作（`purge-expired-data`，365 天；只被「近期」的判斷使用，清除舊紀錄不影響暫停旗標與當日次數）；隱私權政策的保存期限表尚未補上這一列（留給法務覆核） |
| 玩家創作的劇本（含草稿、待審、被退回與舊版本） | 隨帳號刪除；不設期限清除 | 抹除已實作（`CASCADE`，別人的存檔保留並清空來源參照）；「匿名保留已發佈劇本」的偏好屬 RC-8 |
| `llm_call_logs` | 12 個月（不含對話全文） | 已實作（`purge-expired-data`，365 天；用量彙總只讀近期紀錄，不受影響，見 [模型呼叫層-llm-layer.md](模型呼叫層-llm-layer.md)） |
| 失敗的模型原文（`error_detail`） | 14 天，僅管理員可讀 | 僅管理員可讀已實作（C-4b 後台「模型失敗」，讀取端以查詢限制 14 天、不依賴清除排程）；期滿清除已實作（`purge-expired-data` 清為空值，帳本那一筆保留） |
| `auth_tokens` | 到期後 7 天 | 已實作（`purge-expired-data`，不論是否已使用） |
| 管理稽核（`admin_audit_logs`） | 24 個月 | 寫入與檢視已實作（C-4b）；期滿清除已實作（`purge-expired-data`，730 天；唯一的清除途徑在 `RetentionRepository`） |
| 遊戲幣錢包與帳本（`wallets`、`wallet_entries`） | 與帳號相同：帳號刪除時一併刪除，納入個人資料匯出；幣不過期，帳本不設期限清除 | 抹除已實作（`CASCADE`，不匿名化保留）；付款紀錄的法定保存期限待金流規範（W2）時決定 |
| 帳號刪除紀錄（`deletion_tombstones`） | 不設期限（只含內部編號、時間與原因，不含個資） | 已實作；不納入期限清除，備份還原後要靠它重新執行刪除 |
| 應用程式日誌、備份 | 30 天、30 天 | 平台設定（C-5） |

### 告警條件一覽（C-4d）

七項告警（AGENTS.md 第 11 節）依「資料量得到的地方」分成兩邊。**應用程式負責的五項**由 `check-alerts` 評估，門檻是 `app/services/alert_rules.py` 的常數（規範的建議初值，要依實際流量調整；README「營運告警（C-4d）」有逐項的表，並由測試與程式常數比對）：

| 代碼 | 條件 | 嚴重度 | 資料來源 |
|---|---|---|---|
| `usage_budget` | 全站今日 token 或呼叫次數達上限的 80%（警告）、100%（嚴重） | 警告／嚴重 | `llm_call_logs` 彙總與 `USAGE_DAILY_*` |
| `llm_error_rate` | 近 5 分鐘對話呼叫的 429、逾時、伺服器錯誤與網路錯誤占比超過 10%（樣本至少 10 次） | 警告 | `llm_call_logs.status` |
| `llm_config_error` | 近 5 分鐘出現任何一次設定錯誤（金鑰被撤銷等） | 嚴重 | `llm_call_logs.status` |
| `llm_latency_p95` | 近 15 分鐘完成的對話呼叫 p95 超過 10 秒（樣本至少 20 次） | 警告 | `llm_call_logs.latency_ms` |
| `login_lockouts`（顯示名稱「帳號鎖定激增」） | 目前被鎖定的帳號達 5 個：管理員密碼連續失敗（15 分鐘後自動解除）與玩家帳號的自動鎖定（管理員解除前一直計入）合計，同一帳號只算一個 | 警告 | `users.locked_until`、`users.locked_at` |

**平台負責的告警**（資料庫沒有這些資料，或資料庫本身可能就是出問題的地方）。定義由 `scripts/launch_plan.py` 的常數與 `alerts` 子命令產生（日誌型指標、外部探測與 Cloud Monitoring 的告警政策，**預設只預覽**），**門檻與下表由 `tests/test_platform_alerts.py` 比對**；通知管道只有電子郵件，收件人由 `--notify-email` 提供，沒有預設值：

| 告警 | 條件 | 窗口 | 來源 |
|---|---|---|---|
| 5xx 比率 | 伺服器錯誤（5xx）占全部請求**超過 5%**，且窗口內請求數**至少 20**（低流量時不通知） | 300 秒 | Cloud Run 的請求指標 `request_count`，不依賴應用程式的資料庫 |
| 服務存活 | `/healthz` 連續失敗 **3** 次（探測間隔 **1 分鐘**） | 180 秒 | 平台的外部探測（來自服務之外，不需要登入） |
| 服務就緒 | `/readyz` 連續失敗 **3** 次；`/healthz` 正常而它失敗代表資料庫問題，通知標題標明資料庫 | 180 秒 | 平台的外部探測 |
| 寄信失敗 | 日誌事件 `mail.send_failed` 出現 **3** 次以上 | 300 秒 | 日誌型指標（事件不含收件人地址） |
| 排程工作失敗 | 四個排程工作任一個以非零代碼結束 | 300 秒 | Cloud Run Job 的完成次數指標（結果為失敗） |
| 告警檢查停擺 | `check-alerts` 超過 **15 分鐘**沒有成功執行（沒有 `alert.check_completed` 心跳） | 900 秒 | 日誌型指標的缺漏偵測；它自己無法通報自己停擺 |

`check-alerts` 的**非零結束代碼**由上表的「排程工作失敗」接手：資料庫不通時這個指令也跑不動，寄信服務掛掉時它寄不出信，這兩種情況只有平台看得到。管道只有電子郵件（沒有 webhook）；不使用會把例外內容送出本系統的外部錯誤追蹤服務。`usage.global_budget_warning` 日誌事件維持原樣，但**不再是通知來源**。排程：`check-alerts` 每 5 分鐘一次（窗口刻意重疊，短暫的尖峰不會落在兩次檢查之間而漏掉）。

**排程與第一次執行**（排程器設定屬 C-5）：`execute-account-deletions` 與 `purge-expired-data` 各每日一次、可重複執行。第一次在正式環境執行前，先備份資料庫，並各以 `--dry-run` 檢視筆數；`purge-expired-data` 以非零結束代碼回報失敗的類別，排程器應把它接到告警（C-4d）。備份最長 30 天後自然汰除；備份還原後，依 `deletion_tombstones` 與日誌取出的檔案以 `replay-deletions` 重新執行刪除（指令與腳本已實作並在本機 SQL Server 演練，見「資料庫還原與重新抹除」；預備環境的實際演練屬 C-5b）。

**RPG 敘事用途的模型端點**（`OPENROUTER_*`，change `openai-compatible-provider`）：不是 staging 與 production 的必要機密（沒有設定時 RPG 用途不可用：兩個串流端點回 503 `service_unavailable`，唯讀端點照常）；金鑰與主要模型只設一個會啟動失敗。基底網址只允許 https（development 與 testing 另允許回送位址的 http，供本機模型）。免費換付費只改 `OPENROUTER_MODEL`（與選用的 `OPENROUTER_ZDR`）。**推理由 `OPENROUTER_REASONING` 控制**（預設 `false`＝每個請求，含備援模型，都明確帶 `reasoning: {"enabled": false}`；推理型模型預設首字 5 至 35 秒，關閉後不到 1 秒；設為 `true` 前先用 `flask llm-probe` 量首字時間，它會顯示目前的推理設定）。劇情是串流呼叫，**每個進行中的串流多佔一條讀取執行緒**，容量估算見 [模型呼叫層-llm-layer.md](模型呼叫層-llm-layer.md) 第 11 節。**成人模式的部署開關 `ADULT_MODE_ENABLED`**（取代已淘汰的 `SEXUAL_CONTENT_LEVEL3_ENABLED`）預設關閉，各環境規則相同：staging 與 production 須明確設為 `true` 才生效，開啟時 RPG 用途必須已設定，且 `ADULT_MODE_MODELS`（書面許可涵蓋的模型）須涵蓋主要與備援模型，否則啟動失敗（**2026-11-17 規範修訂後不再以書面許可為前提**，這個開關與清單隨 `safe-mode-toggle` 改為緊急煞車，程式尚未跟進）；這只是玩家能使用成人模式的條件之一（玩家仍須已滿 18 歲並完成成年確認）；殘留的舊變數只警告、不開啟任何東西；細節見 [帳號與安全-auth-security.md](帳號與安全-auth-security.md) 第 5 節與 [模型呼叫層-llm-layer.md](模型呼叫層-llm-layer.md) 第 10 節。換成不同模型或供應商時，也要依新方案重設下面的全站用量上限。

全站每日用量上限由環境變數設定（逐項說明在 README 與 `.env.example`）：`USAGE_DAILY_TOKEN_LIMIT`（預設 1,000,000）與 `USAGE_DAILY_CALL_LIMIT`（預設 450）。預設值依目前使用的 `gemini-3.5-flash-lite` 免費層（15 RPM、25 萬 TPM、每日 500 次請求），**換模型或換供應商（例如正式環境改用 OpenAI）時必須重設**；部署時須確認它們與供應商的實際配額一致。

## 6. 測試與驗收分層

```mermaid
flowchart BT
    U["單元與整合測試（pytest）<br/>SQLite 與真實 SQL Server project_test<br/>全部使用假供應商、假寄信器、假時鐘"]
    A["架構測試<br/>分層、無原生 SQL、無供應商字串、外鍵動作、前端規則、設計文件一致性"]
    J["Node 單元測試<br/>前端純函式與同步引擎"]
    B["瀏覽器驗收（Edge CDP）<br/>整頁行為與截圖"]
    C["容器驗收（WSL Docker）<br/>映像與 HTTP 流程"]
    R["真實供應商驗收（需使用者同意）<br/>合成文字、一次性"]
    U --> A --> J --> B --> C --> R
```

**RPG 回合引擎的驗收（rpg-turn-engine，2026-11-10）**：①`tests/` 的端對端旅程（`tests/test_rpg_flow.py`，假供應商，SQLite 與真實 SQL Server 各一次）；②**HTTP 驗收腳本** `scripts/acceptance_rpg_turn.py`（68 項）走真正的網路連線與串流分塊：本機驗收環境（`newprojet-acceptance`，應用程式加假 RPG 模型；假模型與「建立全新玩家」小幫手在 `scripts/rpg_acceptance_harness.py`）與容器內的 gunicorn RPG 串流池各跑一次，腳本只接受本機位址；③**真實模型驗證** `scripts/acceptance_rpg_real_model.py`（第二個應用程式行程用 `.env` 的 OpenRouter 設定，人工執行、不是自動測試，只送劇本內建內容與寫死的示範行動）；結果記在 README 的「驗收紀錄（rpg-turn-engine）」。驗收腳本的約定（動作文字的關鍵字決定假模型的行為）寫在腳本開頭。**記事簿（rpg-notebook）另有 `scripts/acceptance_rpg_notebook.py`**：同一個假模型多一種「記事簿抓取」回應（關鍵字 `測試記事簿超量`／`放棄`／`違規`／`慢速`），小幫手 `GET /fresh/rpg?coins=N` 可補給付費幣（記事簿案例要走 11 至 31 輪，每輪 30 幣）。記事簿腳本的 `eof` 案例量「`done` 到伺服器結束回應」的間隔與結束當下的記事簿；容器驗收（`verify_container.py`）把腳本複製進容器、以 `--assert-eof` 執行，證明 gunicorn gthread 先送出結束 chunk、再執行關閉回呼（2026-11-10：間隔 0.00 秒，結束當下記事簿仍是空的；開發伺服器相同）。真實模型驗證加 `--notebook` 走 11 輪並觀察抓取（結果見 `docs/RPG.md` 7.16.5）。**重新生成與編輯（rpg-turn-revision）另有 `scripts/acceptance_rpg_revision.py`**：同一個假模型依行動文字的關鍵字產生修訂情境（重新生成給出與原回應不同的文字、被擋一次或兩次、中斷、慢速、狀態呼叫失敗；編輯可改關鍵字），腳本對驗收環境的兩個修訂端點檢查事件序列、扣幣與帳本、免費次數（重新生成與編輯共用）、`revision`、失敗保留舊內容與退幣、並行 409 與 `last_turn`，最後掃描伺服器日誌（結果見 README「驗收紀錄（rpg-turn-revision）」）；腳本本身的單元測試在 `tests/test_acceptance_rpg_scripts.py`，日誌事件的列舉與簽名由 `tests/test_rpg_events.py` 守住。**管理員不消耗遊戲幣（admin-unlimited-coins）另有 `scripts/acceptance_rpg_admin_coins.py`**：小幫手 `GET /fresh/rpg?admin=1` 建立全新管理員（角色 admin、不發首登贈幣、餘額 0），腳本沿用修訂腳本的類別驗證三個案例——`journey`（餘額 0 的管理員一輪加兩次免費與三次超過免費次數的修訂，`done.coins` 與餘額始終是 0、記錄頁只有金額 0 的預扣）、`interrupt`（中斷退回後同編號重送，`attempt` 加 1、仍是 0 幣）、`ordinary`（一般玩家用到餘額不足一輪後 `coin_exempt` 為假、再送一輪得到 409 `insufficient_coins`）；改過 `rpg_acceptance_harness.py` 與驗收環境的 `acceptance.py` 之後要重新啟動驗收環境，小幫手才認得 `?admin=1`（結果見 README「驗收紀錄（admin-unlimited-coins）」）。**驗收資料庫有全站每日用量上限（呼叫 450 次、token 100 萬）**：真實模型驗證與假模型驗收共用同一個驗收資料庫，一次 11 輪的真實驗證是 25 次呼叫（審查 1、第 0 幕 1、串流與狀態各 11、抓取 1），加上當天先前的假模型與真實模型驗收，就會用盡（症狀：開存檔 503 `review_unavailable`、回合 `system_busy`，日誌的原因為 `budget`）；重跑前以環境變數 `USAGE_DAILY_CALL_LIMIT`、`USAGE_DAILY_TOKEN_LIMIT` 啟動驗收環境調高上限。**Windows 上用 `localhost` 連本機伺服器會先試 IPv6 而多等約 2 秒**（驗收腳本因此直接連 127.0.0.1）。

最近一次的數字（2026-10-30，C-5a）：pytest 5522 項（含真實 SQL Server，沒有略過；上次 5241 項）、Node 175 項、瀏覽器驗收 C-3 的 64 項、C-4a 的 30 項、C-4b 的 100 項加 C-4c 的 48 項；C-4d 沒有新的前端，改以真實的 SMTP 擷取伺服器加真實的 SQL Server 驗收寄信；C-5a 同樣沒有前端變動，改以本機 SQL Server 實跑還原演練、本機冒煙跑一次負載測試；容器驗收 95 項是 C-3 時的結果，之後沒有重跑。細節與已知限制記在 README 的「客服對話（C-3）」、「用量限制（C-4a）」、「管理員後台（C-4b）」、「個資生命週期（C-4c）」與「營運告警（C-4d）」。後台的瀏覽器驗收需要預先塞好資料的暫時伺服器（被擋的使用者、意見回饋、失敗原文、7 日用量），驗收腳本不進版本庫，結果與修正記在 README。**備份腳本的陷阱**：升級資料庫前的 `BACKUP` 必須讀完所有結果集再向伺服器確認檔案存在，否則驅動程式關閉游標時伺服器會靜默中止備份（見 README「管理員後台（C-4b）」的驗收紀錄）。真實的 Gemini 呼叫只在人工驗收進行，且每次都需要使用者當下同意（免費方案的資料可能被用於改進產品，只用合成文字）。

### 負載測試（C-5a）

目的：在不消耗模型額度、不危及正式環境的前提下量測 worker、執行緒、資料庫與限流的容量（report.md D-24）。工具是 Locust（`requirements-dev.txt`，**只在開發環境，不進映像**），檔案在 `loadtest/`（Dockerfile 不複製、`.dockerignore` 排除）。

| 項目 | 說明 |
|---|---|
| 假模型入口 | `loadtest/fake_app.py`：以 `LoadTestProvider` 取代真實供應商（真的睡眠來模擬延遲，不連線、不耗額度）。`LOADTEST_LATENCY_SECONDS`（預設 1.5）、`LOADTEST_JITTER_SECONDS`（0.5）、`LOADTEST_ERROR_RATES`（例如 `rate_limited=0.02,timeout=0.01,server_error=0.01`，總和不得超過 1）、`LOADTEST_SEED`。**`APP_ENV` 為 `staging`、`production` 或未設定時載入即失敗**；預設關閉信箱驗證，註冊不寄信。啟動：`python -m flask --app loadtest.fake_app run --port 5000`（`APP_ENV=testing`，`DATABASE_URL` 指向 SQLite 檔案） |
| 情境 | `loadtest/locustfile.py`：每位虛擬使用者帶著**預先簽出的登入 cookie** 進入（玩家只用 Google 登入，沒有註冊與密碼登入可用；cookie 由 `scripts/mint_test_sessions.py` 在目標環境內建立測試玩家並簽出，寫成 `test-sessions.json`，路徑由 `LOADTEST_SESSIONS` 指定，**數量不足就在送出任何請求之前失敗**）、開新對話，之後每 30 至 45 秒送一則訊息，並混入列表與 `since_seq` 增量拉取。每人自己的帳號（保留網域 `@loadtest.invalid`），每個非 GET 請求都帶同源 `Origin` 與 CSRF 權杖。簽出步驟拒絕在 `production` 執行；映像不新增任何能簽發登入狀態的指令或端點。**本機（`--local`）簽出的 cookie 只有目標伺服器使用相同的 `SECRET_KEY` 與 `DATABASE_URL` 才會被接受**（腳本讀得到 `.env`，`loadtest.fake_app` 不讀，所以兩邊都明確設定；對容器用 `--docker-exec`） |
| 執行 | `locust -f loadtest/locustfile.py --headless -u 100 -r 5 -t 15m --host http://127.0.0.1:5000 --csv loadtest/result`（預設建議 100 位虛擬使用者、15 分鐘；找拐點用下面的階梯式情境）。**降級回覆（模型暫時性錯誤且備援也失敗，HTTP 仍是 200）在 Locust 裡標為失敗**，所以模型錯誤會進錯誤率 |
| 位址守門 | 預設只對本機位址執行；其他位址需要 `--confirm-remote`；**正式環境一律拒絕**（即使有確認參數）：`loadtest/guard.py` 的 `PRODUCTION_DOMAINS`（取得正式網域後填入；目前尚未取得網域，所以是空的）與任何主機名稱含 `production` 的位址 |
| 判定 | `python loadtest/evaluate.py --csv-prefix loadtest/result --log app.log [--cpu-percent 55]`：**p95 小於 8 秒**（對話訊息的請求）、**錯誤率小於 1%**、日誌**沒有資料庫鎖定錯誤**（SQLite `database is locked`、SQL Server 死結 1205 與鎖逾時 1222；應用程式日誌依規範不記例外訊息，所以真實日誌裡以 `"error_type":"OperationalError"` 計入，這是唯一看得到的資料庫層錯誤）、**CPU 低於 70%**（由平台量測後填入；**未提供就標為「未驗證」，整體不判為通過**）。結束代碼 0 全部通過、1 有項目未通過、2 沒有未通過但有未驗證 |
| 階梯式情境（找拐點） | `loadtest/shapes.py`（純邏輯，不匯入 Locust）加薄包裝 `loadtest/locustfile_stairs.py`：`LOADTEST_STAIRS=start=20,step=20,steps=5,seconds=120`（第一階段 20 位，每階段加 20 位，共 5 階段，每階段 120 秒，共 10 分鐘；`steps` 至少 2、`seconds` 至少 10、最高 5000 位）。執行：`locust -f loadtest/locustfile_stairs.py --headless --host http://127.0.0.1:5000 --csv loadtest/result --csv-full-history`。每階段開頭用最多 10 秒把新增的使用者逐步加進來，階梯結束後 Locust 自行停止（不需要 `-t`）。設定不合法就以非零碼結束並說明欄位規則，不回顯值 |
| 假模型伺服器（隔離的壓測環境） | `loadtest/fake_llm/`：Gemini 相容的 `generateContent`，讓**真實的** Gemini 轉接器經 `GEMINI_BASE_URL` 指向它，整條程式路徑都是真的、只有模型是假的。只接受部署時設定的測試金鑰 `FAKE_LLM_API_KEY`（不符回 403、看起來像真實金鑰的值拒絕啟動），延遲與錯誤率沿用 `LOADTEST_LATENCY_SECONDS`、`LOADTEST_JITTER_SECONDS`、`LOADTEST_ERROR_RATES`、`LOADTEST_SEED`，另有 `LOADTEST_TIMEOUT_SECONDS`（逾時類錯誤睡多久，預設 25 秒），日誌只有請求數與各狀態碼的彙總。有自己的 Dockerfile 與 `requirements.txt`（只有 Flask 與 gunicorn），**不屬於主映像**；`GEMINI_BASE_URL` 在 production 設定了會啟動失敗。本機執行：`python -m flask --app "loadtest.fake_llm.server:create_fake_llm_app" run --port 8081`（先設定 `FAKE_LLM_API_KEY`） |
| 每一輪的記錄 | `evaluate.py` 加 `--environment local｜local-container｜cloud`、`--setting WEB_CONCURRENCY=2`（可重複，名稱限於 `WEB_CONCURRENCY`、`GUNICORN_THREADS`、`CPU`、`MEMORY`、`CONCURRENCY`、`MIN_INSTANCES`、`MAX_INSTANCES`、`USERS`、`DURATION_SECONDS`）、`--stairs`（與 `LOADTEST_STAIRS` 同格式）；輸出環境、容量依據、設定、負載，最後印出可貼進 README 壓測紀錄表的「記錄列」 |

**只在測試或預備環境執行，不在正式環境壓測**；結果回頭調整 D-11、D-12、D-13（worker 與執行緒數）。

**本機結果的解讀**：gunicorn 不支援 Windows，本機只能用 Flask 的 threaded 伺服器，和正式環境的 gunicorn gthread 不同；本機容器（WSL 的 Docker）雖然用 gunicorn，卻是 SQLite、不同的 CPU 與記憶體，也不是 Cloud Run。所以**本機與本機容器的數字只用來確認測試本身可用、判定腳本正確、假模型與錯誤率設定生效、映像在 Linux 與 gunicorn 下確實能跑、`WEB_CONCURRENCY` 與 `GUNICORN_THREADS` 確實生效，以及取得粗略的相對比較（例如加倍執行緒是否改善 p95）——不是容量依據**。真正的容量量測在雲端預備環境（`cloud-staging-run`），gunicorn 的預設值到那時才依實測調整。

**階梯式加壓與每一輪的記錄規則**

1. 從基準設定開始，每一輪**只調整一個變數**（`WEB_CONCURRENCY`、`GUNICORN_THREADS` 或資源大小），跑一次完整的階梯，用 `evaluate.py` 判定，把「記錄列」貼進 README 的壓測紀錄表。
2. 每一輪的記錄都要有：執行環境（本機、本機容器或雲端）、虛擬使用者數與持續時間（階梯的各階段）、gunicorn 的 worker 與執行緒數、資源設定（本機容器的 CPU 與記憶體限制，或雲端的 CPU、記憶體、並行數與執行個體數）、判定結果與各項數字，以及**這個數字是否可作為正式環境的容量依據**。
3. **只有雲端的結果可以作為容量依據**；本機與本機容器一律標為「不是容量依據」，`evaluate.py` 在沒有標明環境時也不當作容量依據。本機的結果不會讓「負載測試」關卡變成已通過。
4. 最終採用的設定要附上對應那一輪的結果；所有嘗試的設定都達不到通過標準時，記錄目前最佳的結果與瓶頸，關卡維持「進行中」。

| 日期 | 環境 | 伺服器與資源設定 | 負載（階梯） | 各項數字與判定 | 整體 | 容量依據 |
|---|---|---|---|---|---|---|
| （由 `evaluate.py` 的「記錄列」貼入） | | | | | | |

### 本機預演（C-5b）

使用者目前沒有可計費的 Google Cloud 專案，所以預備環境的**應用程式行為**先在本機預演（不花錢）：`APP_ENV=staging`、SQLite 檔案、經 ngrok 的 https 網址。啟動仍通過與雲端相同的設定驗證（缺機密即失敗、`staging` 信任入口層標頭、不得使用 `sa` 與 `TrustServerCertificate=yes`），**預演不放寬任何 staging 的檢查**；它也**不需要** `TRUST_PROXY_HEADERS`（那個開關只給 development，見「開發用通道（ngrok）」）。

**步驟**（Git Bash，在專案根目錄；`.env.staging` 與 `staging.vars` 被版本控制忽略，值不要印出或貼到對話）

1. 載入設定：`set -a; . ./.env.staging; . ./staging.vars; set +a`，再 `export APP_ENV=staging DATABASE_URL=sqlite+pysqlite:///staging-rehearsal.sqlite3 BASE_URL=https://<你的固定網域>`（SQLite 檔案在被忽略的 `*.sqlite3`）。**每開一個新的終端機分頁都要重新載入**：沒有載入時 `alembic` 與 `flask run` 不會報錯，而是悄悄退回讀專案的 `.env`，把遷移與伺服器指到開發用的 SQL Server（2026-11-03 實際發生過一次，見 README「驗收紀錄（remove-player-password-auth）」）。
2. 建立資料表：`python -m alembic upgrade head`（staging 不使用 `create_all`）。**先看輸出的第一行是 `Context impl SQLiteImpl`**，不是 `MSSQLImpl`，才算跑在預演資料庫上。
3. 建立首位管理員，**在你自己的終端機互動輸入密碼，不經對話**：`python -m flask --app wsgi create-admin`。
4. 啟動：`python -m flask --app=wsgi run --port 5000`（內建的 `run` 不是專案的自訂指令）（Windows 沒有 gunicorn，本機預演才有這個例外；容量以容器預演與雲端為準），另開終端機 `ngrok http --url=<你的固定網域> 5000`。啟動前先故意拿掉一個必要機密（例如 `unset SECRET_KEY`），確認啟動失敗且訊息只列變數名稱，再補回。
5. 冒煙驗收：`python scripts/staging_smoke.py https://<你的固定網域>`（見「預備環境驗收」）。
6. Google 登入人工驗收（首次、第二次、同信箱既有帳號的合併）與營運告警信的真實寄送：在預演的資料庫製造一項告警條件（例如 5 個被鎖定的 `.invalid` 測試帳號），執行 `python -m flask --app wsgi check-alerts`，由使用者確認收到信（含是否進垃圾郵件匣）。沒收到就標為「未驗證」並寫明檢查過的地方與推測原因，**不以預期結果代替**；驗證後清除測試帳號與告警狀態。
7. （條件式）本機容器預演：WSL 的 Docker 能啟動時，建置主映像、以 gunicorn gthread 啟動、`GEMINI_BASE_URL` 指向假模型伺服器，跑階梯式加壓並以 `evaluate.py --environment local-container` 判定；起不來就標為「未預演」並寫明原因，**不以 Flask 開發伺服器的數字代替**。
8. 收尾：關閉通道、刪除預演的 SQLite 檔案與測試資料、確認 `.env.staging` 與 `staging.vars` 仍未被追蹤（`git status` 沒有任何預演產物）。

**預演驗證了什麼、哪些必須等雲端**（README 的驗收紀錄照這兩欄分開寫）

| 本機預演可以驗證 | 必須在雲端驗證（預演不能代替；在 `cloud-staging-run` 完成前維持「尚未做」） |
|---|---|
| 啟動時的設定驗證與缺機密即失敗 | 託管資料庫的私有連線與伺服器憑證驗證 |
| HSTS、其餘四個安全標頭、沒有 CSP | Cloud Run 服務與 Secret Manager 的注入方式 |
| `__Host-` 前綴與 Secure cookie、CSRF 與來源檢查（經 https 網域） | Cloud Scheduler 與 Cloud Run 工作（排程指令的實際觸發） |
| 冒煙驗收腳本的全部檢查 | 平台告警（日誌型指標、政策）與演練 |
| Google 登入與帳號合併（人工） | 雲端的壓測容量（含 CPU 由 Cloud Monitoring 量測） |
| 營運告警信經 Brevo 的真實寄送（人工） | 雲端的還原演練（Cloud SQL 複製） |
| 映像在 Linux 與 gunicorn 下能跑、gunicorn 兩個旋鈕生效（條件式） | 託管資料庫的成本與備份／時間點還原的實際行為 |

**上線前關卡不因本機預演而通過**：「託管資料庫成本」與「負載測試」兩道關卡的狀態在本機預演之後維持原狀。

### 預備環境驗收（C-5b）

`python scripts/staging_smoke.py https://<預備環境網址>`：對一個預備環境網址依序檢查並逐項輸出「通過」、「失敗」或「略過」，本機預演的通道網址與日後的雲端網址使用同一個腳本。結束代碼：沒有失敗為 0、有失敗為 1、位址被拒絕為 2。

| # | 檢查項目 | 預期 |
|---|---|---|
| 1 | `/healthz` | 200 |
| 2 | `/readyz` | 200 |
| 3 | 首頁的安全標頭 | 五個標頭齊全（含 `Strict-Transport-Security`），沒有 `Content-Security-Policy` |
| 4 | 沒有 CSRF 權杖的 POST | 403 |
| 5 | 已移除的 `POST /api/auth/login` 與 `/api/auth/register` | 404（玩家只用 Google 登入；帶有效的 CSRF 權杖，才不會被 403 蓋過） |
| 6 | 帶測試玩家的登入 cookie 進入（`SMOKE_SESSION_COOKIE`） | 成功；**沒有提供 cookie 時標為「未驗證」，不是通過** |
| 7 | 開新對話 | 成功（需要登入；否則未驗證） |
| 8 | session 與 CSRF cookie | `__Host-` 前綴、`Secure`、不含 `Domain`、`Path=/`；session 為 `HttpOnly`（需要登入；否則未驗證） |
| 9 | 一般會員存取 `/api/admin/overview` | 404（與不存在的端點相同，不透露後台存在；需要登入；否則未驗證） |
| 10 | 未登入存取 `/api/cs/conversations` | 401 |
| 11 | 模型呼叫 | **預設略過**；加 `--allow-model-call` 才送出一則訊息並檢查回覆不是降級文案（會消耗模型額度） |

- **只對 https 的預備環境執行**：主機名稱含 `production` 或位於 `loadtest/guard.py` 的 `PRODUCTION_DOMAINS` 時一律拒絕，**沒有任何確認參數可以繞過**；非 https、帶帳密、路徑、查詢字串或片段的位址也拒絕，而且是在送出任何請求之前。
- **輸出只有項目名稱與結果**：沒有 cookie、權杖、密碼或回應本文；失敗只說明是哪個標頭、狀態碼或屬性。
- **預備環境不放真實個資**：帳號只能是保留的 `.invalid` 網域的測試帳號（RFC 2606，永遠不會有真實信箱），資料庫不得由正式環境複製而來，部署與還原流程沒有把正式資料還原到預備環境的步驟。冒煙腳本每執行一次會留下一個這樣的測試帳號。
- 經 ngrok 的網址，腳本會多帶 `ngrok-skip-browser-warning` 標頭略過免費方案的瀏覽器警告頁。
- Google 登入需要使用者本人的 Google 帳號，只能**人工驗收**；沒有人工驗收就標為「未驗證」，不以自動化測試或預期結果代替。

**驗收紀錄的格式**（寫在 README）：日期、對應的 commit、每一項的結果與數字、未完成或未驗證的項目，並分成「本機預演已驗證」與「必須在雲端驗證（尚未做）」兩欄。上線前關卡的狀態**只在有對應紀錄時**才更新為「進行中」或「已通過」，且要記錄資料來源的位置；沒有實際執行的項目標為未驗證。
- 驗證：`tests/test_staging_smoke.py`（以 `https://` 的 staging 測試應用驅動全部檢查、缺 HSTS 或 `__Host-` 各別失敗、CSRF 沒擋住會失敗、預設不送訊息、輸出掃描、位址守門）。

## 7. 營運手冊與上線前關卡（C-5a）

> 本節的指令與腳本都真實存在，由 `tests/test_launch_docs.py` 逐一檢查；關卡的狀態只在有依據時才標為「已通過」。部署與回復見第 2、3 節的「部署與回復」，還原見「資料庫還原與重新抹除」，平台告警見「告警條件一覽」。

### 機密輪替

至少每 **90 天**輪替一次，疑似外洩時**立即**輪替（AGENTS.md 第 9 節）。步驟固定：**建立新金鑰 → 更新機密 → 重新部署 → 驗證 → 停用舊金鑰**。

| 機密 | 用途 | 建立新金鑰 | 備註 |
|---|---|---|---|
| `GEMINI_API_KEY` | 客服（只有客服使用 Gemini） | Google AI Studio 建立新金鑰 | 驗證：`/readyz` 與一則真實客服對話；之後才停用舊金鑰 |
| `OPENROUTER_API_KEY` | RPG 用途（與 `OPENROUTER_MODEL` 成對，不使用 Gemini） | OpenRouter 建立新金鑰 | RPG 尚未對外，仍照排程輪替 |
| `SECRET_KEY` | session 與 CSRF 簽章 | 產生新的 32 字元以上隨機值 | **先**把舊值放進 `SECRET_KEY_FALLBACKS`（Flask 3.1 以上）再換新值，部署並驗證後才移除舊值，否則所有人會被登出 |
| `DATABASE_URL` | 資料庫連線（帳號與密碼） | 在 Cloud SQL 建立新密碼 | 不得使用 `sa`；變更後 `/readyz` 必須回 200 |
| `MAIL_SMTP_PASSWORD` | 寄信服務 | 寄信服務建立新密碼 | 驗證：忘記密碼流程寄出一封測試信 |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Google 登入 | Google Cloud 主控台建立新的用戶端密鑰 | 開發與正式使用不同的 OAuth 用戶端 |

- **更新機密**：`python scripts/gcp_launch.py secrets --project … --region … --env production --sql-instance … --rotate GEMINI_API_KEY --execute`（`--rotate` 只為指定的機密寫入新版本，可重複；值從標準輸入讀取，不經命令列；服務以 `latest` 參照）。不加 `--rotate` 的 `secrets` 是第一次準備（建立服務帳號、機密容器與權限），會對所有必要機密寫入版本，輪替時不要用。
- **重新部署**：`python scripts/gcp_launch.py deploy … --execute`（新版本的修訂才會讀到新的機密），並驗證 `/healthz` 與 `/readyz`。
- **停用舊金鑰**：在供應商端停用；Secret Manager 中舊版本改為停用，不刪除，直到確認沒有問題。
- 輪替日期與下次到期日記在營運的行事曆上（不寫進程式庫）。

### 事故處理

| 事故 | 怎麼發現 | 立即動作 | 後續 |
|---|---|---|---|
| **金鑰外洩**（任何機密） | 供應商通知、用量異常（`usage_budget`）、`llm_config_error` | **立即輪替**（上表）；在供應商端先停用外洩的金鑰；檢視 `check-alerts` 輸出與用量是否異常 | 找出外洩途徑；確認日誌沒有機密（日誌白名單與掃描測試保證不記錄）；記錄事件 |
| **模型供應商故障** | `llm_error_rate`、`llm_latency_p95`、對話出現降級文案 | 不需要重新部署：客服已有備援模型與降級文案；確認 `GEMINI_FALLBACK_MODEL` 有效；RPG 看 `OPENROUTER_FALLBACK_MODEL` | 供應商恢復後告警自動恢復；若長時間故障，通知使用者（站內公告屬後續功能） |
| **資料庫不通** | `/readyz` 失敗（`/healthz` 正常）、5xx 比率、`check-alerts` 非零結束 | 查看 Cloud SQL 狀態與連線；排程工作會失敗並由平台告警通知 | 必要時依「資料庫還原與重新抹除」還原；**還原後一定要執行 `replay-deletions` 才能開放流量** |
| **被攻擊**（登入暴力嘗試、註冊洪水、請求洪水） | `login_lockouts`、5xx 比率、用量異常 | 帳號層級的鎖定與每分鐘訊息數已在運作；必要時用 `flask --app wsgi check-alerts --dry-run` 檢視；在平台層暫時限制流量 | **入口層沒有 IP 限流（見下方「未滿足的規範項目」）**：這是目前最大的缺口，上線前必須重新決定 |
| **資料毀損或誤刪** | 使用者回報、資料異常 | 停止流量；依還原步驟處理 | 還原後執行 `replay-deletions`，演練見 `scripts/restore_drill.py` |

### 上線前關卡

對外開放真實會員前必須全部通過。狀態只有「未開始」「進行中」「已通過」；**標為「已通過」必須在「依據」欄寫明日期與驗收紀錄的位置**，沒有依據不得標為已通過。

| 關卡 | 負責人 | 狀態 | 依據／備註 |
|---|---|---|---|
| 營運告警與管理員重設密碼的寄信設定（`ALERT_EMAIL_TO` 與 `MAIL_*` 已設定，寄信服務已選定並實際寄出過一封） | 使用者（寄信服務）、系統（staging 與 production 缺少設定時啟動失敗） | 未開始 | 寄信服務尚未選定（營運告警信目前經 Brevo SMTP 寄給使用者本人）；玩家端不寄任何信，所以不再有「信箱驗證」關卡 |
| 模型供應商方案與資料使用條款的重新評估（客服的 Gemini 與 RPG 的 OpenRouter 各自評估，金鑰分別為 `GEMINI_API_KEY` 與 `OPENROUTER_API_KEY`） | 使用者 | 未開始 | 目前是個人測試用的免費方案，不得輸入真實個資 |
| 網域取得，Google 登入與信件的 SPF／DKIM／DMARC 設定 | 使用者 | 未開始 | 尚未取得網域；取得後填入 `loadtest/guard.py` 的 `PRODUCTION_DOMAINS` |
| 隱私權政策、服務條款與保存期限的法務覆核 | 使用者 | 未開始 | 政策頁已描述實際行為，尚未經法務 |
| 託管資料庫的成本評估與同意（N-9） | 使用者 | 未開始 | 本機開發使用本機 SQL Server；Cloud SQL 費用未評估；使用者目前沒有可計費的 Google Cloud 專案（2026-11-02），雲端部分屬 C-5c |
| 負載測試達到通過標準 | 系統（腳本）、使用者（預備環境） | 進行中 | 測試與判定腳本已完成；2026-11-02 本機容器預演留下兩輪記錄（README「驗收紀錄（C-5b）」），**兩輪都不是容量依據**也都沒有通過；雲端的實測屬 C-5c（`cloud-staging-run`） |
| 入口層 IP 限流的決定 | 使用者 | 未開始 | 2026-10-30 使用者決定先不做；上線前必須重新決定（見下方） |
| 成年驗證方式（只有要開放成人模式時才需要） | 使用者 | 未開始 | 方式已決定為切換時勾選確認（report.md 的 U-28），尚待法務確認足夠；程式已改為三種內容模式（`rpg-data-and-script`），成人模式的部署開關 `ADULT_MODE_ENABLED` 預設關閉。**2026-11-17 規範修訂**：全站只限滿 18 歲（新使用者註冊時勾選聲明），關閉安全模式不再以書面許可或法務意見為前提，部署層開關改為緊急煞車；劇情輸出的審查層（RC-2）已實作。這一列隨 `safe-mode-toggle` 重新評估 |

### 未滿足的規範項目

目前**尚未滿足**、刻意延後的項目；每一項都載明原因、風險與何時重新評估，不得悄悄消失。

| 項目 | 規範來源 | 原因 | 風險 | 重新評估 |
|---|---|---|---|---|
| **入口層 IP 限流** | AGENTS.md 第 9 節「限流第一道使用入口層」 | 使用者決定先不做（Cloud Run 沒有內建；Nginx 旁車或 Cloud Armor 加外部負載平衡器各有複雜度與費用） | 入口沒有第一道防線：只有帳號層級的登入鎖定與每分鐘訊息數，擋不住來自單一 IP 的註冊洪水、請求洪水與對不存在帳號的暴力嘗試 | 上線前關卡「入口層 IP 限流的決定」；C-5b 之前 |
| **內容安全政策（CSP）** | 未明文要求，屬安全加固；列在此以免遺忘 | 前端使用 Vue 全域建置，模板在瀏覽器編譯，政策必須允許 `unsafe-eval`，幾乎沒有保護力（見「傳輸安全與安全標頭」） | 若出現 XSS，沒有第二道防線；目前靠純文字渲染（嚴禁 `v-html`）與輸入驗證 | 前端改為預先編譯模板時 |
| **RPG 專用的獨立 worker 池（Google Cloud 部分）** | AGENTS.md 第 9 節「客服與 RPG 串流使用獨立的 worker 池」 | `gunicorn.rpg.conf.py` 與串流端點（`/api/game/`）已完成（rpg-turn-engine）。**AWS 展示環境已達成**（2026-11-17，task 6.1、6.3）：兩個池各用自己的設定檔、只綁回送位址，入口依路徑分流並驗證了暫停客服池時 RPG 池仍回應。Google Cloud 的 production 還沒有分流（沒有可計費的專案）；執行緒數與 worker 數仍是初始值，展示環境的數字不是容量依據 | 在 production 分流之前，長連線的串流會佔用客服的執行緒 | Google Cloud 部署時（Cloud Run 第二個服務或入口層路徑規則）與 `rpg-ops-loadtest`（壓測） |
| **平台告警尚未在 Google Cloud 建立；AWS 展示環境只做了其中一部分** | AGENTS.md 第 11 節（五項告警之外的三項由平台負責） | Google Cloud：定義已寫成腳本（預設只預覽），沒有雲端專案可建立。AWS 展示環境：只有三件事（`check-alerts` 計時器、EC2 狀態檢查告警、Budgets），其餘缺口逐項列在第 8 節「展示環境未達 production 的缺口」 | 上線後沒有人會被通知 5xx、存活與寄信失敗 | Google Cloud 的預備環境建立時；展示環境的各項見第 8 節 |

## 8. AWS 展示環境（aws-demo-environment，RC-4 前半）

> **狀態：計畫模組、執行器與資料庫初始化／探測腳本已完成並有測試。`build_plan` 的各組與 `host-init` 已於 2026-11-16 至 11-17 在 AWS 雪梨的測試帳號上實際執行並逐項驗證（task 4.2 至 4.5，資源仍在計費）；`deploy_plan` 的 `build`、`db-init`、`snapshot`、`migrate` 組與 `refresh-parameter-DATABASE_URL` 已於 2026-11-17 對真實主機與 RDS 執行並驗證（task 5.1、5.3、5.4：衍生映像層方案成立，連線只有 `Encrypt=yes`、憑證由映像的系統憑證庫驗證，見下方「資料庫憑證方案」；快照、遷移到 `0018` 與種子見「資料庫初始化與連線探測」一節末）；`deploy_plan` 的 `restart`、`verify` 組已於 2026-11-17 對真實主機執行並驗證（task 6.1：客服池與 RPG 池啟動，見「啟動兩個池」一節）；`deploy_plan` 的 `ingress`、`routing` 組已於 2026-11-17 對真實主機執行並驗證（task 6.2、6.3：Caddy 啟動並簽發憑證、HTTP/2、導向、安全標頭、暫停客服池驗證分流，見「啟動入口與排程」「驗證分流」兩節）；串流探測（task 6.4，不是計畫步驟）已對真實入口與真實模型執行：入口沒有緩衝串流、心跳未驗證，見「串流探測」一節；`deploy_plan` 的 `schedule` 組與告警、寄信也已對真實的主機與 AWS 驗證（task 7.1 至 7.3：五個計時器、EC2 狀態告警演練、營運告警信寄達但進垃圾郵件匣，見「啟動入口與排程」「告警與寄信」兩節）；冒煙驗收與真實模型連通性（task 8.1、8.3）見「冒煙驗收與真實模型連通性」一節；Google 登入人工驗收（task 8.2，四項通過，並發現 5.5 的管理員第一次沒有建成、已重建）見「Google 登入人工驗收」與「建立首位管理員」兩節；adult-only-signup 的部署（映像 `87ebcd8`、遷移到 `0019`）與 Google 登入五個情境的人工驗收（2026-11-18，全部通過）見「adult-only-signup 的部署與驗收」一節；未達 production 的缺口見最後一節「展示環境未達 production 的缺口」；`rollback_plan`、`teardown_plan` 尚未對真實 AWS 執行；DuckDNS 的更新由使用者在自己的終端機執行過（結果以 DNS 與對外連線驗證，沒有看到執行器的輸出）**；雲端驗收（task 8.1 至 8.4）已全部完成，仍未驗證的項目列在 README「驗收紀錄（aws-demo-environment）」的驗收摘要與本節最後一節。本節描述計畫模組 `scripts/aws_demo_plan.py` 產生的內容、執行器 `scripts/aws_demo.py` 的用法，以及在主映像內執行的 `scripts/aws_demo_bootstrap_db.py` 與 `scripts/aws_demo_probe_db.py`；實際執行時發現的差異會先回寫計畫與測試。

展示環境是 `APP_ENV=staging` 的單機部署：Google Cloud 的部署（第 2、3 節、`cloud-staging-run`）因為沒有可計費的專案而擱置，展示改用 AWS 雪梨（`ap-southeast-2`，Free plan 抵用金 US$100，展示期到 2026-12-31）。它的目的是讓兩個 worker 池第一次真正分流、讓 `rpg-ops-loadtest` 有量測的基礎。

**限制（不得誤當成 production）**：它不是 production，資源名稱與網域都不含 `production`（計畫模組在建立時就拒絕）；不含真實會員資料（只用 `.invalid` 的測試帳號）；單機單點、記憶體與延遲的數字**不是容量依據**；備份保留、日誌保存與平台告警都**未達** production 的要求，缺口明細與重新評估時機見本節最後的「展示環境未達 production 的缺口」（task 7.4）。AGENTS.md 第 9 節的「雲端平台為 Google Cloud」描述的是 production，本環境不修改它。

### 拓撲

```mermaid
flowchart LR
    Visitor["瀏覽器"] -->|"HTTPS 443、HTTP 80<br/>rac001.duckdns.org"| Eip["Elastic IP"]
    Eip --> Caddy
    subgraph Host["EC2 t3.small（Amazon Linux 2023；沒有 SSH，只開 80 與 443）"]
        Caddy["rac-caddy<br/>Caddy 2、host 網路<br/>憑證存 /var/lib/rac/caddy"]
        Cs["rac-cs 客服池<br/>127.0.0.1:8081"]
        Rpg["rac-rpg RPG 串流池<br/>127.0.0.1:8082"]
        Jobs["rac-job-*.timer<br/>排程（一次性容器）"]
        Env["/run/rac/*.env<br/>tmpfs、0600、機密"]
    end
    Caddy -->|"其餘路徑"| Cs
    Caddy -->|"/api/game/*<br/>flush_interval -1，不壓縮"| Rpg
    Cs --> Rds
    Rpg --> Rds
    Jobs --> Rds
    Rds[("RDS SQL Server Express<br/>db.t3.micro，不公開、加密")]
    Ssm["SSM Parameter Store<br/>/rac-demo/staging/*"] -->|"啟動前取出"| Env
    Source["S3 傳檔儲存貯體<br/>git archive 的壓縮檔"] -->|"主機上 docker build"| Host
    Operator["管理者"] -->|"SSM Session Manager"| Host
    Watch["Budgets 60% 與 90%<br/>EC2 狀態告警"] --> Sns["SNS 電子郵件"]
```

| 元件 | 實體與設定 | 產生它的函式（`scripts/aws_demo_plan.py`） |
|---|---|---|
| 入口 | `rac-caddy.service`：`caddy:2`、host 網路、`admin off`、唯讀根檔案系統、只保留綁 80 與 443 的 capability；`/api/game/*` 到 RPG 池並關閉回應緩衝，其餘到客服池；不對任何路徑壓縮；憑證與帳戶資料存持久路徑 `/var/lib/rac/caddy` | `caddyfile()`、`caddy_unit()` |
| 客服池、RPG 池 | `rac-cs.service`（`gunicorn.conf.py`、埠 8081）、`rac-rpg.service`（`gunicorn.rpg.conf.py`、埠 8082）：只發布在回送位址、非 root、`--read-only`、`--cap-drop=ALL`、`no-new-privileges`、日誌循環、`Restart=always` | `pool_unit()` |
| 版本標籤 | `/etc/rac/release.env`（`RAC_IMAGE_TAG=<提交短碼>`）；部署前把目前的標籤存成 `release.previous.env`；容器一律 `--pull=never`，標籤不存在就失敗 | `release_env_file()`、`_switch_release_lines()` |
| 機密 | 存在 SSM Parameter Store（SecureString，`/rac-demo/staging/<名稱>`）；每個單元一支取出腳本（`rac-render-env-app`、`-bootstrap`），只取該單元需要的名稱，寫到 tmpfs 的 `/run/rac`；應用程式單元**不含** RDS 主要使用者密碼；一般設定在 `/etc/rac/plain.env`（非機密） | `render_env_script()`、`plain_env_file()` |
| 排程 | `rac-job-<指令>.service／.timer`：由 `SCHEDULED_COMMANDS` 逐項產生（只支援 `*/N * * * *`、`0 * * * *`、`M H * * *`，其他形式丟錯）；時區 `Asia/Taipei`、`Persistent=true`；手動類指令不排程 | `cron_to_on_calendar()`、`scheduled_units()` |
| 資料庫 | RDS for SQL Server Express `db.t3.micro`：單一可用區、20 GB gp3、加密、不公開、備份保留 1 天（Free plan 不接受 7 天，AWS 回 `FreeTierRestrictionError`；規格只要求大於 0）、只允許主機安全群組連入；主要使用者 `racmaster`（不是 `sa`），應用程式之後只用專屬登入 | `build_plan()` 的 `rds` 與 `rds-ready` 組 |
| 映像 | 在主機上以 `git archive <提交短碼>` 的壓縮檔（經 S3）分兩段建置：共用的 `Dockerfile` 建成基底映像 `rac-app-base:<提交短碼>`，再疊一層主機上的 RDS CA 套件（衍生 Dockerfile，見「資料庫憑證方案」）成為 `rac-app:<提交短碼>`；不含 `.env`，以非 root（uid 10001）執行；前一版映像保留供回復 | `deploy_plan()`、`derived_dockerfile()` |
| 預算與告警 | Budgets：季度 US$75，實際花費 60% 與 90% 兩個門檻寄信；CloudWatch：EC2 狀態檢查連續失敗通知 SNS 主題；收件人由呼叫者提供，沒有預設值 | `build_plan()` 的 `budget` 與 `alarms` 組 |

**入口設定、憑證與分流的實際結果（task 6.2、6.3、6.4，2026-11-17；一覽，逐項的做法、證據與限度見「啟動入口與排程」「驗證分流」「串流探測」三節）**：

| 項目 | 設定 | 在真實環境量到的結果 |
|---|---|---|
| 憑證 | 由 Caddy 自動簽發與管理（簽發者是 Let's Encrypt），帳戶與憑證存在主機的 `/var/lib/rac/caddy`（掛進容器；設計上換容器不會重新簽發，**重啟 Caddy 後不重新簽發沒有驗證**，目前只有第一次啟動的紀錄） | 簽發者 `CN=YE2`、到期 2027-02-15，**啟動 Caddy 後約 15 秒簽發完成**；以系統憑證庫驗證通過（沒有 `-k`）。**續期沒有驗證**：到期日在展示期（到 2026-12-31）之後，展示期內不會發生續期 |
| HTTPS、HTTP/2 | 入口在 443 提供 HTTPS | 外部（使用者的電腦）ALPN 協商 `h2`、TLS 1.3；主機內 `curl` 也協商到 HTTP/2 |
| HTTP 導向 | 80 導向同網域的 HTTPS | 回 **308**，導向 `https://rac001.duckdns.org/healthz` |
| 標頭 | 應用程式自己送 HSTS 與四個安全標頭（見「傳輸安全與安全標頭」），入口不改寫 | 經入口仍在（`max-age=31536000`）；回應帶 `Via: 1.1 Caddy`、`Server: gunicorn`；`/healthz` 與 `/readyz` 本文只有 `{"status":"ok"}`，沒有內部細節 |
| 路徑分流 | `/api/game/*` 到 RPG 池（`127.0.0.1:8082`，關閉回應緩衝、不壓縮），其餘到客服池（`127.0.0.1:8081`） | 暫停客服池時，非 `/api/game/` 的路徑經入口回 502、`/api/game/sessions/1` 仍回 401（RPG 池處理）；恢復後三條路徑回到 200、401、401。客服在暫停期間不可用，不超過該命令的約 40 秒 |
| 串流不被緩衝 | `flush_interval -1`、不壓縮 | 第 0 幕的第一個事件 0.78 秒到達、`done` 6.11 秒：**入口沒有緩衝**（HTTP/1.1、一次成功執行、一個來源）；**心跳經入口的到達時間未驗證** |
| 外部直連 | 安全群組只開 TCP 80 與 443 | 從使用者的電腦連 Elastic IP：80、443 連得上，8081、8082、22、1433 逾時（被丟棄，不是明確拒絕；只有一個來源） |
| 觀察（沒有修改） | — | 回應帶 `Alt-Svc: h3=":443"`，但沒有開 UDP 443，瀏覽器會退回 HTTP/2；要關閉 HTTP/3 或開 UDP 443 由使用者決定 |

主機上要放的檔案共 17 個（`host_files()`：Caddyfile、`plain.env`、兩支取出腳本、三個常駐單元、五個排程的各一對 `.service`／`.timer`）。**主機初始化**（建立 `/etc/rac` 與 `/var/lib/rac/caddy`、2 GB 交換檔、安裝並啟動 docker、放置 RDS 的 CA 套件、經 SSM 放置上述檔案、`systemctl daemon-reload` 與啟用單元、停用主機內的 sshd）沒有預先寫成計畫步驟（使用者 2026-11-16 決定）：task 4.4 在 SSM 上現場撰寫並逐項驗證，task 4.5 回寫成 `build_plan` 的 `host-init` 組（見下方「主機初始化」一節）；部署計畫假設這一組已執行完成。

### 步驟總表

四份計畫各自獨立、步驟 id 不重複（執行器以 id 選單步）。下表與 `scripts/aws_demo_plan.py` 產生的步驟逐項一致，`tests/test_design_docs.py` 會比對；以預設的目標（一個通知信箱、沒有額外機密）為準。

| 計畫 | 組 | 步驟 id | 做什麼 |
|---|---|---|---|
| `build_plan` | preflight | `check-identity` | 確認目前的 AWS 帳號就是預期的帳號，不符即停止 |
| `build_plan` | budget | `create-budget`、`create-sns-topic`、`subscribe-sns-0`、`confirm-sns-subscriptions` | 預算與兩個門檻、SNS 主題、每個通知信箱一個訂閱（`subscribe-sns-<N>`）、人工確認訂閱信 |
| `build_plan` | network | `get-default-vpc`、`create-host-sg`、`authorize-host-ingress`、`create-db-sg`、`authorize-db-ingress` | 預設 VPC；主機安全群組只開 80 與 443；資料庫群組只允許主機群組 |
| `build_plan` | iam | `create-host-role`、`attach-ssm-managed-policy`、`put-role-policy`、`create-instance-profile`、`add-role-to-profile`、`wait-instance-profile` | 主機角色（SSM 受管政策加只讀本專案參數路徑與傳檔儲存貯體的內嵌政策）與設定檔 |
| `build_plan` | storage | `create-bucket`、`block-bucket-public-access`、`encrypt-bucket`、`tag-bucket` | 傳檔儲存貯體：封鎖公開、加密、標籤 |
| `build_plan` | secrets | `put-parameter-SECRET_KEY`、`put-parameter-GEMINI_API_KEY`、`put-parameter-RDS_MASTER_PASSWORD`、`put-parameter-DB_APP_PASSWORD` | SSM 參數（已存在不覆寫；值來自 `--secrets-file` 或隨機產生，不經命令列）；另有 `--secrets-file` 額外指定的機密 |
| `build_plan` | rds | `get-default-subnets`、`create-db-subnet-group`、`create-rds-instance` | 只建立、不等待（開始計費，需要 `--accept-cost`） |
| `build_plan` | ec2 | `get-ami`、`launch-ec2`、`wait-ec2-running`、`allocate-eip`、`get-eip-address`、`associate-eip`、`wait-ssm-online` | RDS 建立的等待期間建立主機（IMDSv2 強制、無金鑰對）與 Elastic IP（需要 `--accept-cost`） |
| `build_plan` | rds-ready | `wait-rds-available`、`get-rds-endpoint`、`put-parameter-DATABASE_URL`、`refresh-parameter-DATABASE_URL` | 等 RDS 可用、驗證不公開與加密，再寫入 `DATABASE_URL`（連線字串只有 `Encrypt=yes`）；`refresh-…` 是覆寫步驟：既有的參數仍是舊型態（帶 `ServerCertificate`）時，重新組成並覆寫（值經暫存檔、不顯示；驗證只看名稱與型別；只限由腳本組成的機密） |
| `build_plan` | alarms | `create-ec2-status-alarm` | EC2 狀態檢查告警 |
| `build_plan` | host-init | `host-init-find-host`、`host-init-system`、`wait-host-init-system`、`host-init-ca`、`wait-host-init-ca`、`host-init-files`、`wait-host-init-files`、`host-init-units`、`wait-host-init-units`、`host-init-secrets`、`wait-host-init-secrets`、`host-init-sshd`、`wait-host-init-sshd` | 主機初始化（見下一節）：找出主機，再依序 A 系統、B 憑證、C 檔案、D 單元、E 機密取出檢查、F 停用 sshd；不需要 `--accept-cost`，放置檔案需要 `--vars-file` |
| `deploy_plan` | prepare | `deploy-find-host` | 以名稱與標籤找出主機，必須剛好一台 |
| `deploy_plan` | snapshot | `snapshot-db`、`wait-snapshot-db` | 遷移之前的 RDS 快照（每個提交短碼一個，已存在就沿用），等到 available |
| `deploy_plan` | build | `archive-source`、`upload-source`、`build-image`、`wait-build-image`、`check-image`、`wait-check-image` | `git archive` 取出已提交的內容、上傳到傳檔儲存貯體、在主機上分兩段建置映像（基底映像，再加一層 RDS CA 的衍生映像）並檢查（非 root、沒有 `.env` 與 `.git`、系統憑證庫的憑證數＝基底映像的張數＋RDS 套件的張數） |
| `deploy_plan` | db-init | `deploy-find-db-endpoint`、`upload-ops-bootstrap-db`、`upload-ops-probe-db`、`bootstrap-db`、`wait-bootstrap-db`、`probe-db`、`wait-probe-db` | **只在第一次部署以 `--group db-init` 執行**（例行部署的整份計畫不含它）：唯讀取得 RDS 端點、把兩支維運腳本加密上傳到傳檔儲存貯體的 `ops/` 前綴（主機下載後比對 SHA-256）、用 RDS 主要使用者在 `rac-app:<標籤>` 的一次性容器內建立專屬資料庫與登入（可重複執行），再以應用程式登入的連線字串探測；兩個等待步驟驗證標準輸出 `docker-exit=0`（容器的輸出在標準錯誤）；探測沒有通過就停止，不放寬 |
| `deploy_plan` | migrate | `migrate-db`、`wait-migrate-db`、`check-migration-head`、`wait-check-migration-head`、`seed-scripts`、`wait-seed-scripts` | 一次性容器執行 `alembic upgrade head`、確認已是最新版、再執行 `seed-scripts`；失敗即停止，不重啟 |
| `deploy_plan` | create-admin | `check-create-admin`、`wait-check-create-admin`、`create-admin`、`cleanup-create-admin`、`wait-cleanup-create-admin` | **只在第一次部署以 `--group create-admin` 執行**（例行部署的整份計畫不含它）：先在主機上乾跑 `create-admin --help`（同一個映像、強化與環境檔，不詢問、不寫入資料庫，輸出 `create-admin-help=ok`）；再是人工步驟 `create-admin`（只列出要在 SSM 終端機輸入的兩行指令，執行器不執行它，`--yes` 不能代替，信箱、顯示名稱與密碼都由終端機詢問，密碼不回顯）；最後清理互動容器與機密環境檔，輸出 `app-env-left=0 container-left=0` |
| `deploy_plan` | restart | `switch-release`、`wait-switch-release` | 換 `release.env`（前一版存成 `release.previous.env`）並只重啟兩個池 |
| `deploy_plan` | verify | `check-pools`、`wait-check-pools`、`check-pool-hardening`、`wait-check-pool-hardening` | 兩個池在回送位址上 `/readyz` 回 200，且都在跑新映像；再唯讀驗證執行方式（task 6.1）：行程都以 UID 10001（非 root）執行、各自只執行自己的 gunicorn 設定（客服池 `gunicorn.conf.py`、RPG 池 `gunicorn.rpg.conf.py`）、容器埠只發布在回送位址且主機上該埠只有 `127.0.0.1` 的監聽；全部符合才輸出固定的一行，任何一項不符就在標準錯誤印出被比對的值並失敗 |
| `deploy_plan` | ingress | `start-ingress`、`wait-start-ingress`、`check-ingress`、`wait-check-ingress` | **只在第一次部署以 `--group ingress` 執行**（例行部署的整份計畫不含它；task 6.2）：拉取 `caddy:2`、重啟 `rac-caddy.service` 並等容器進入執行狀態（輸出 `ingress=running`）；再在主機內以 `curl --resolve 網域:埠:127.0.0.1` 連回送位址（仍經 Caddy）驗證：HTTPS 在最多 150 秒內以有效憑證回 200 且是 HTTP/2、HTTP 導向同網域的 HTTPS、HSTS 與四個安全標頭經入口仍在；全部符合才輸出 `ingress tls=ok http=2 redirect=https headers=5`，憑證的簽發者與到期日印在標準錯誤供記錄 |
| `deploy_plan` | routing | `check-routing`、`wait-check-routing` | **只在第一次部署以 `--group routing` 執行**（例行部署的整份計畫不含它：它會暫停客服池，客服在暫停期間不可用；task 6.3）：先確認兩個池在回送位址上 `/healthz` 都回 200、入口的三條路徑都有正常的應用程式回應（`/healthz` 200，`/api/cs/conversations` 與 `/api/game/sessions/1` 未登入 401）；再 `systemctl stop rac-cs.service`，在暫停中經入口確認兩條非 `/api/game/` 的路徑回 502（客服池處理、它沒人在聽）、`/api/game/` 路徑仍回 401（RPG 池處理），RPG 池的 `/healthz` 仍回 200、客服池的回送位址連不上；接著 `systemctl start` 並等客服池 `/readyz` 回 200（最多 150 秒），三條路徑回到暫停前的狀態碼；任何一步失敗或被 `HUP`、`INT`、`TERM` 中斷都先把客服池啟動再結束；全部符合才輸出 `routing game=rpg other=cs paused=502 rpg=200 restored=ok` |
| `deploy_plan` | schedule | `start-timers`、`wait-start-timers`、`run-check-alerts`、`wait-run-check-alerts`、`check-schedule`、`wait-check-schedule` | **只在第一次部署以 `--group schedule` 執行**（例行部署的整份計畫不含它；task 7.1）：啟動五個排程計時器（輸出 `timers-active=5`）、手動跑一次 `check-alerts` 服務（輸出 `check-alerts result=success status=0`，失敗時印出該單元最後 20 行日誌）、唯讀核對作用中的計時器恰好等於排程清單且都有下一次觸發時間、`systemctl --failed` 沒有任何失敗單元（輸出 `timers=5 next=5 failed=0`） |
| `rollback_plan` | prepare | `rollback-find-host` | 找出主機 |
| `rollback_plan` | restart | `rollback-switch-release`、`wait-rollback-switch-release` | 換回前一版標籤並重啟兩個池；映像不在主機上就在改任何檔案前失敗；**不執行任何遷移或降級、不還原資料庫** |
| `rollback_plan` | verify | `rollback-check-pools`、`wait-rollback-check-pools`、`rollback-check-pool-hardening`、`wait-rollback-check-pool-hardening` | 同部署的驗證 |
| `teardown_plan` | compute | `td-delete-ec2-alarm`、`td-terminate-ec2`、`td-wait-ec2-terminated`、`td-release-eip` | 告警、主機、Elastic IP |
| `teardown_plan` | database | `td-delete-rds`、`td-wait-rds-deleted`、`td-delete-db-subnet-group` | RDS（必須明確選擇是否保留最終快照）與子網路群組 |
| `teardown_plan` | snapshots | `td-delete-snapshots` | 部署前快照：沒給名單時是沒有指令的佔位；使用者看過清單並明確回覆要保留的例外之後，才改為對名單內的每個快照產生 `td-delete-snapshot-<名稱>`（最終快照永遠不刪） |
| `teardown_plan` | network | `td-delete-db-sg`、`td-delete-host-sg` | 兩個安全群組（資料庫群組先刪） |
| `teardown_plan` | iam | `td-remove-role-from-profile`、`td-delete-instance-profile`、`td-delete-role-policy`、`td-detach-ssm-policy`、`td-delete-role` | 角色與設定檔 |
| `teardown_plan` | storage | `td-delete-bucket`、`td-delete-parameter-SECRET_KEY`、`td-delete-parameter-DATABASE_URL`、`td-delete-parameter-GEMINI_API_KEY`、`td-delete-parameter-RDS_MASTER_PASSWORD`、`td-delete-parameter-DB_APP_PASSWORD` | 傳檔儲存貯體（含物件）與每個機密參數 |
| `teardown_plan` | notify | `td-delete-sns-topic`、`td-delete-budget` | SNS 主題與預算（最後刪，拆除期間費用告警仍有效） |
| `teardown_plan` | verify | `td-list-remaining-snapshots`、`td-wait-tagged-resources-gone` | 列出仍保留的手動快照；等帶標籤的資源清空並逐一確認各服務沒有殘留 |

### 計畫的共同規則

- **區域與名稱守門**：`Target` 建立時就檢查，區域不是 `ap-southeast-2`、帳號不是 12 位數字、網域或任何資源名稱含 `production` 都拒絕，所以不可能產生任何指令；每個 `aws` 指令都明確帶 `--region=ap-southeast-2`。
- **費用護欄**：建立 RDS、EC2 與 Elastic IP 的步驟在沒有 `--accept-cost` 時只留說明、不輸出指令；執行器顯示的預估來自 `cost_lines()`（約 US$1.6／日，到 2026-12-31 約 US$75；牌價粗估，以 AWS 帳單頁為準）。
- **機密**：計畫裡只有機密的名稱與參照（`{{secret:名稱}}`，只出現在經暫存檔傳入的 JSON 內），沒有值；傳給 AWS 的 JSON 一律只用 ASCII（Windows 的 AWS CLI 以系統地區編碼讀 `file://`）。
- **在主機上執行的命令**走 SSM Run Command：`send-command` 輸出命令編號，接著 `wait-…` 步驟輪詢到 `Success`，`Failed`、`Cancelled`、`TimedOut` 立即停止（不等逾時）；主機沒有 SSH。
- **部署順序固定為快照 → 建置 → 遷移 → 重啟 → 驗證**：快照 available 之後才遷移；遷移與種子都成功才重啟兩個池（失敗時舊版仍在跑，遷移是 expand-only，舊版程式可在新結構上運作）；驗證在最後。映像標籤就是提交短碼（7 至 40 位小寫十六進位），所以每個映像都對得到提交。**第一次部署**在建置與遷移之間多一組 `db-init`（資料庫初始化與憑證探測，用 `--group db-init` 明確執行）：它不在例行部署的整份計畫內（`ROUTINE_DEPLOY_GROUPS`），所以例行部署不會每次都用 RDS 主要使用者的密碼；執行器的步驟目錄（`--only` 與擷取值的唯讀取回）則含它。第一次部署在遷移與重啟之間還有一組 `create-admin`（人工建立首位管理員，見下方「建立首位管理員」），同樣不在例行部署內。**驗證之後**還有兩組同樣只在第一次部署以 `--group` 明確執行的 `ingress`（啟動 Caddy 並驗證）與 `schedule`（啟動排程計時器並驗證）：兩個池要先回 200 才啟動入口；例行部署重啟的只有兩個池，入口與計時器一直在跑，所以例行部署的驗證仍在最後（`FIRST_DEPLOY_GROUPS` 含 `db-init`、`create-admin`、`ingress`、`schedule` 四組）。
- **回復只換標籤**：`release.env` 換回前一版並重啟兩個池，沒有降級遷移、不還原資料庫。
- **拆除只動帶標籤的資源**：每個刪除步驟先以帶 `Project=rac-demo` 與 `Environment=staging` 兩個標籤的查詢找出資源（`run_if_found`），沒有符合就略過；RDS 刪除要求明確選擇保留或不保留最終快照，**沒有預設值**（沒選時不輸出刪除指令）。**手動快照**含資料庫內容、不隨 RDS 消失且持續計費，所以另有規則（design D14）：不論選擇保留或不保留最終快照，執行器先唯讀列出（`snapshot_listing_step()`）該資料庫仍存在的快照，使用者必須明確回覆要保留的**例外**（「沒有例外」也要明說，沉默不算），沒有被指定保留的**部署前快照**（`<資料庫>-pre-<提交短碼>`）才預設刪除；名單由 `snapshots_to_delete()` 算出（例外打錯名稱會丟錯，不會悄悄變成全部刪除），傳給 `teardown_plan(delete_snapshots=…)`，沒給名單時 `td-delete-snapshots` 沒有指令；每個刪除仍先以標籤查詢把關。**最終快照（本次保留的與先前拆除留下的）永遠不由計畫刪除**，要刪由使用者自己處理，拆除最後會列出仍留著的快照。
- **已驗證與未驗證**：計畫的輸出、順序與守門由 `tests/test_aws_demo_plan.py` 驗證；每條 `aws` 指令都以假金鑰離線確認過引數與 JMESPath 查詢能被 AWS CLI 解析、主機腳本通過 `bash -n`。`build_plan` 的各組與 `host-init` 已對真實的 AWS 與主機（Amazon Linux 2023 的 docker、systemd）執行並逐項驗證，差異已回寫（task 4.2 至 4.5，見下一節與 README「驗收紀錄（aws-demo-environment）」）；`deploy_plan` 的 `build` 組（兩段建置與 `check-image`）與 `db-init` 組、`refresh-parameter-DATABASE_URL` 已對真實主機與 RDS 執行並驗證（task 5.3，結果見下方「資料庫憑證方案」；`db-init` 回寫後的步驟另對真實主機重跑一次，兩個等待步驟都是 `docker-exit=0`）；`deploy_plan` 的 `snapshot`、`migrate` 組也已對真實的 AWS 與 RDS 執行並驗證（task 5.4）；`deploy_plan` 的 `restart`、`verify` 組也已對真實主機執行並驗證（task 6.1，兩個池啟動；原有的兩個步驟沒有發現需要回寫的差異，之後補進 `verify` 組的 `check-pool-hardening` 第一次執行時發現並修正了 `docker top` 的參數，見「啟動兩個池」一節）；`deploy_plan` 的 `ingress` 與 `routing` 組也已對真實主機執行並驗證（task 6.2、6.3，Caddy 已在主機上運作；兩組第一次執行都沒有需要回寫的差異，見「啟動入口與排程」「驗證分流」兩節）；`rollback_plan`、`teardown_plan` **沒有對真實的 AWS 執行過**，DuckDNS 的更新由使用者在自己的終端機執行（沒有看到執行器的輸出，只以結果驗證）；`schedule` 組已於 2026-11-17 對真實主機執行並通過（task 7.1，見「啟動入口與排程」一節），告警與寄信的演練見「告警與寄信」一節；串流探測腳本已於 2026-11-17 對真實入口與真實模型執行（task 6.4：入口沒有緩衝串流，心跳未驗證，見「串流探測」一節）；指令與旗標的差異在後續雲端批次回寫。

### 主機初始化（`build_plan` 的 `host-init` 組）

task 4.4 在主機上以 SSM 現場撰寫並逐項驗證，task 4.5 回寫成計畫步驟（腳本模板與步驟在 `scripts/aws_demo_plan.py` 的「主機初始化」一節，測試在 `tests/test_aws_demo_plan.py`）。順序固定，每一段一個 SSM 命令加一個等待步驟；腳本只印出**一行固定文字**，等待步驟以它驗證（不符就停止）。

| 步驟 | 做什麼 | 驗證的輸出 |
|---|---|---|
| `host-init-find-host` | 以名稱與標籤找出主機（唯讀、必須剛好一台）；實例編號存成 `host-instance-id`，**不依賴建立主機的步驟**，所以單步或另一次執行都不需要 `--accept-cost` | 查詢結果為 1 台 |
| A `host-init-system` | `/etc/rac`（0755）與 `/var/lib/rac/caddy`（0700）；2 GB 交換檔（先寫成 `/swapfile.new` 再改名，不留下半成品；寫入 fstab 一次）；`dnf install docker`、啟用並啟動 docker | `docker=active swap=1 dirs=755,700` |
| B `host-init-ca` | 以 HTTPS（TLS 1.2 以上）取 AWS 公開的 `…/ap-southeast-2-bundle.pem`，至少一張憑證且能解析才放到 `/etc/rac/rds-ca-bundle.pem`（0644；之後只是映像建置的輸入，容器不掛載它） | `ca-bundle=644` |
| C `host-init-files` | 放置 `host_files()` 的 17 個檔案：內容以 base64 傳（含繁體中文，不直接放進 JSON）、附 sha256；安裝前比對雜湊（不符就在安裝之前失敗）、安裝後再比對一次並檢查權限 | `files=17` |
| D `host-init-units` | `daemon-reload`；**啟用但不啟動**兩個池、Caddy 與 5 個排程計時器（排程的 `.service` 由計時器觸發，不單獨啟用） | `enabled=8` |
| E `host-init-secrets` | 以主機角色執行 `rac-render-env-app`，確認取得全部應用程式機密、`/run/rac` 是 tmpfs 且權限為 700／600；**只印數量，不印任何值**，結束時（含失敗）立即刪除環境檔 | `secrets=<數量> fs=tmpfs mode=700/600` |
| F `host-init-sshd` | 停用主機內的 sshd（Amazon Linux 2023 預設啟用並監聽 `0.0.0.0:22`）；之後的管理只走 SSM | `sshd=inactive port22=0 ssm=active` |

- **可重複執行**（回寫後的版本已在真實主機以執行器重跑一次，六段的固定輸出行全部符合）：A 做過的不重做、B 與 C 重新放置相同的內容、D 與 F 的啟用與停用沒有副作用；任何一行失敗就停止（`set -eu`）。**沒有任何一段啟動 `rac-*` 單元**（兩個池由部署與重啟啟動、Caddy 在 6.2、計時器在 7.1），測試掃描所有腳本確認。腳本只用 ASCII、沒有機密名稱與值（機密只在 E 於主機上取出並在檢查後刪除）。
- **一般設定檔（`--vars-file`）**：`plain.env` 的內容來自一般設定（非機密，例如 `aws-demo.vars`，`*.vars` 已被 `.gitignore` 忽略），C 因此需要 `provision --vars-file …`；沒有給時 C 只留說明、不輸出指令，執行到那裡以代碼 4 停止（A、B 照常）。檔案只能放一般設定：含機密變數、已淘汰的變數、不認得的變數或缺少必要設定都在呼叫任何指令之前被拒絕，訊息只有變數名稱。
- **SSH 與「沒有 22」**：規格的「沒有 22」指對外——安全群組沒有 22 的規則、沒有金鑰對，所以外部連不進來；Amazon Linux 2023 的 sshd 預設仍在主機內監聽，F 把它停用，讓「只能經 SSM 連入」在主機內也成立。
- **雲端執行時發現的差異（2026-11-17，已回寫）**：①AL2023 的 `/tmp` 是記憶體檔案系統，暫存檔一律放 `/var/tmp`（2 GB 的主機不要把大檔放在 `/tmp`）；②RDS 的 CA 套件含 3 張憑證（第一張 `Amazon RDS ap-southeast-2 Root CA RSA2048 G1`，2061 年到期），RDS 實際使用的 CA 是 `rds-ca-rsa2048-g1`，與套件吻合；③docker 25.0.x、系統內建 `aws-cli/2.38`，不需另外安裝；④Windows 上的 Git Bash 會把以 `/` 開頭的命令列引數改寫成 Windows 路徑，手動查 SSM 參數要設 `MSYS_NO_PATHCONV=1`（執行器以 `subprocess` 呼叫不受影響）；⑤執行器在 Windows 把輸出重設為主控台編碼，導向檔案時是 cp950，要讀輸出請設 `PYTHONUTF8=1`；⑥`--only` 不能與 `--group` 同用。
- **仍未驗證**：`systemd-analyze verify` 只在現場驗證過（對我們的單元沒有警告），沒有寫進計畫步驟；Caddy 與兩個池在主機上的實際啟動。

### 執行器（`scripts/aws_demo.py`）

執行器只負責預覽、確認、執行與驗證，步驟的內容與順序都在計畫模組。**預設只預覽，不呼叫任何外部指令**（包含 `aws` 與 `git`）；加上 `--execute` 才會逐步執行，每一步執行前列出該步驟（指令、暫存檔內容、輪詢與驗證）並等待確認，**拒絕就停止**（結束代碼 3）。外部指令一律以引數清單傳遞（沒有 shell）；區域不是 `ap-southeast-2` 時，在呼叫任何指令之前就被拒絕；`--account-id` 沒有預設值，執行前先用 `aws sts get-caller-identity` 比對目前登入的帳號，不符即停止。

| 子命令 | 做什麼（對應的計畫） | 範例 |
|---|---|---|
| `provision` | 建立環境（`build_plan`）：`--group` 只做某幾組、`--secrets-file` 提供機密的值、`--vars-file` 提供一般設定（主機初始化放置 `plain.env` 用）、`--accept-cost` 同意建立計費資源 | `python scripts/aws_demo.py provision --account-id 123456789012 --notify-email ops@example.com --accept-cost --execute` |
| `deploy` | 部署（`deploy_plan`）：`--tag` 是提交短碼，省略時取目前 git 的短提交；第一次部署用 `--group` 分組執行（`build`，然後 `db-init`，再 `snapshot`、`migrate`、`create-admin`；`restart`、`verify` 留到 `create-admin` 之後）；沒指定 `--group` 時是例行部署，不含 `db-init`、`create-admin` | `python scripts/aws_demo.py deploy --account-id 123456789012 --tag abc1234 --execute` |
| `rollback` | 回復（`rollback_plan`）：只把兩個池換回舊標籤，不動資料庫 | `python scripts/aws_demo.py rollback --account-id 123456789012 --tag abc1234 --execute` |
| `teardown` | 拆除（`teardown_plan`）：必須明確選 `--final-snapshot` 或 `--no-final-snapshot`（互斥、沒有預設值；真正執行時沒選會在呼叫任何指令前被拒絕） | `python scripts/aws_demo.py teardown --account-id 123456789012 --no-final-snapshot --execute` |
| `duckdns` | 把展示網域指向帶標籤的 Elastic IP；權杖只從環境變數 `DUCKDNS_TOKEN` 讀取 | `python scripts/aws_demo.py duckdns --account-id 123456789012 --execute` |

- **單步執行**：`--only STEP`（步驟代碼見預覽或上面的步驟總表）只處理那一步；`--only STEP --yes` 執行並**不再詢問**確認（確認由呼叫者針對該步驟明確給出）；`--yes` 只能與 `--only` 一起用，沒有「整份計畫一律同意」；`--only` 與 `--group` 不能一起用。
- **取回先前步驟的結果**：步驟之間以 `{{名稱}}` 傳遞擷取值（例如 `vpc-id`）。單步或分組執行時，缺少的值只用**唯讀**的方式向 AWS 取回（建立資源的步驟只重跑它的「已存在」查詢，純查詢的步驟重跑自己）；SSM 命令編號之類無法安全取回的值會要求先執行產生它的步驟，不會為了取值而重複建立任何東西。
- **`--accept-cost`（只有 `provision`）**：沒有它時，建立 RDS、EC2 與 Elastic IP 的步驟只留說明、不輸出指令，預覽仍會顯示費用預估，真正執行到那一步會**停止**（結束代碼 4）；有它時，建立計費資源前先顯示預估（約 US$1.6／日、到 2026-12-31 約 US$75；牌價粗估，以 AWS 帳單頁為準），之後仍逐步確認。`--only … --yes` 也需要它。
- **`--vars-file`（只有 `provision`）**：一般設定（非機密，`KEY=VALUE`、UTF-8、不做變數插值；建議檔名 `aws-demo.vars`，`*.vars` 已被 `.gitignore` 忽略），主機初始化的 `host-init-files` 用它產生 `/etc/rac/plain.env`。沒有它時那一步只留說明、不輸出指令，執行到那裡以代碼 4 停止；有它時，檔案在呼叫任何指令之前先檢查（不得含機密變數、已淘汰與不認得的變數，必要設定要齊全），訊息只有變數名稱，預覽顯示「一般設定檔：N 個設定」。
- **機密**：值來自 `--secrets-file`（`KEY=VALUE`、UTF-8、**不做變數插值**；建議檔名 `aws-demo.secrets`，`*.secrets` 已被 `.gitignore` 忽略）或由腳本隨機產生（`SECRET_KEY` 64 位十六進位、資料庫兩個密碼 40 字且只含英數與 `-_`）；`DATABASE_URL` 由腳本用 RDS 端點與專屬登入組成（只有 `Encrypt=yes`，**沒有 `ServerCertificate`、沒有 `TrustServerCertificate`**，憑證由映像的系統憑證庫驗證；密碼經 URL 編碼）。檔案只能放來自你的機密：由腳本產生或組成的名稱與未知的名稱會被拒絕，選取的步驟需要的機密缺漏時列出名稱（不顯示任何值）。值只經暫存檔（權限限本人、**用後一定刪除，含失敗與中斷**）交給 `aws ssm put-parameter --cli-input-json file://…`，不進命令列、輸出與日誌，所有輸出另外經過遮蔽（出現已知的機密值就以 `***` 取代）。**只有建立該參數的步驟能產生新值**；其他步驟（例如建立 RDS 用的主要使用者密碼）一律從 SSM 讀回已存在的值，讀不到就失敗，絕不為已存在的資源另外產生一個沒有存下來的密碼。已存在的參數不覆寫，唯一的例外是 `refresh-parameter-DATABASE_URL`：它只覆寫由腳本組成的機密（`DATABASE_URL`，其他名稱在計畫模組就被拒絕，執行器也只對它重新組成值），資料庫登入密碼從 SSM 讀回、不重新產生；`put-parameter` 的覆寫不能同時指定標籤（AWS 會拒絕），所以請求不含標籤、原有的標籤保留；驗證只查名稱與型別（`describe-parameters`），計畫內沒有任何 `--with-decryption`，值不顯示。
- **輪詢**：有間隔與上限，逾時時說明等待了多久與最後一次的狀態；命令錯誤視為「還沒好」（例如 SSM 命令剛送出時 `InvocationDoesNotExist`）；SSM 命令進入 `Failed`、`Cancelled`、`TimedOut` 立即停止，並印出該命令錯誤輸出的前 500 字（主機腳本的訊息是英文、不含機密）。
- **DuckDNS**：用標準函式庫呼叫 `https://www.duckdns.org/update`；權杖來自環境變數 `DUCKDNS_TOKEN`，不經命令列、不傳給 `aws` 與 `git` 子行程，任何輸出與例外都不含權杖或含權杖的網址（例外只記錄類別）；先唯讀查出 Elastic IP，顯示後由你確認才更新，只接受單層的 `*.duckdns.org` 子網域。2026-11-17 由使用者在自己的終端機執行過（task 4.1；`DUCKDNS_TOKEN` 只存在他的終端機，我沒有看到執行器的輸出），`rac001.duckdns.org` 因此指向 Elastic IP `16.179.97.189`，我以 `nslookup` 與對該網域的 HTTPS 連線確認了結果。
- **結束代碼**：0 成功；1 失敗（指令失敗、驗證不符、逾時）；2 用法錯誤（區域、帳號、未選最終快照等，沒有呼叫任何指令）；3 你拒絕或沒有明確回覆；4 步驟被擋下（沒有 `--accept-cost`、沒有 `--vars-file`、尚未選擇最終快照）。
- **Windows**：子行程設定 `AWS_CLI_FILE_ENCODING=UTF-8`，暫存檔以 UTF-8 寫入；主控台是 cp950 時不能編碼的字元以 `?` 取代，不會因此中斷。

**人工確認點**（執行器不會替你做的事）：

| 步驟 | 要你做什麼 |
|---|---|
| `confirm-sns-subscriptions` | 每位收件人開啟 AWS 寄的訂閱確認信並點擊確認（沒收到先看垃圾郵件匣）；`--yes` 不能代替人工動作，此步驟一律詢問 |
| `create-rds-instance`、`launch-ec2`、`allocate-eip` | 開始計費：看過費用預估並加上 `--accept-cost`，再逐步確認 |
| `host-init-*` | 不需要你動手，但每一步仍逐步確認（或你針對該步驟明確給 `--only … --yes`）；放置檔案要提供 `--vars-file`，看過內容再同意；`host-init-sshd` 會停用主機內的 sshd（之後只能用 SSM 管理） |
| `create-admin` | 由你在 SSM 終端機自己執行並輸入信箱、顯示名稱與密碼，不經腳本與對話（task 5.5）；執行器只列出要輸入的兩行指令並等你確認，`--yes` 不能代替 |
| `duckdns` | 確認查到的 Elastic IP 正確，才會更新網域 |
| `teardown` 的保留或不保留最終快照 | 明確選擇 `--final-snapshot` 或 `--no-final-snapshot`，沒有預設值 |
| `teardown` 的快照例外 | 見下：必須明確回覆，「沒有例外」也要明說 |

**拆除時的快照流程（design D14）**：真正執行 `teardown` 時，執行器在**任何刪除之前**先唯讀列出該資料庫的手動快照，顯示部署前快照（預設會刪除）與其他快照（例如最終快照，永遠不刪），然後要求你回覆要保留的例外：輸入要保留的快照名稱（空白或逗號分隔），或輸入「沒有例外」；**沉默、直接按 Enter 都不算回覆**，名稱打錯會說明原因並重問，三次沒有明確回覆就停止（結束代碼 3，沒有刪除任何東西）。回覆之後才以 `snapshots_to_delete()` 算出名單、重新產生計畫（`td-delete-snapshot-<名稱>`），之後照一般拆除逐步確認；每個快照刪除前仍先以標籤查詢把關。`--yes` 不能略過這個問題；`td-delete-snapshots`（沒有指令的佔位）不能用 `--only` 執行。預覽（不加 `--execute`）不呼叫 AWS，所以只列出「列出快照」的指令與被擋下的佔位步驟。

**已驗證與未驗證（執行器）**：預覽不呼叫任何指令、拒絕即停止、單步只執行一步、被擋下的步驟不執行、輸出與引數沒有機密值、暫存檔一定刪除、輪詢逾時與失敗狀態、DuckDNS 權杖不外洩、拆除不會刪到未標籤的資源與被保留的快照，都由 `tests/test_aws_demo_script.py` 以假的 runner 驗證。**執行器對真實 AWS 的執行（task 4.x 至 7.x）**：`build_plan` 的各組與 `deploy_plan` 的 `snapshot`、`build`、`db-init`、`migrate`、`create-admin`、`restart`、`verify`、`ingress`、`routing`、`schedule` 都已經由執行器對真實的 AWS、主機與 RDS 執行並逐項驗證，實際的 `aws` 回應形狀、錯誤文字、耗時與暫存檔在真實 CLI 的差異都已回寫（例如 Free plan 不接受 7 天的備份保留、`ServerCertificate` 與 RDS 不相容，見各節與 README「驗收紀錄（aws-demo-environment）」）。**沒有對真實 AWS 執行過**：`rollback` 與 `teardown` 兩份計畫的執行路徑，以及 DuckDNS 步驟（由使用者在自己的終端機執行，沒有看到執行器的輸出，只以 DNS 與對外連線驗證）。

### 資料庫憑證方案（design D7，2026-11-17 修訂）

RDS 的伺服器憑證由 AWS 的 RDS CA 簽發，不在 Debian 的系統憑證庫內；RDS 端點的主機名稱與憑證相符，所以只缺信任錨。連線字串**只有 `Encrypt=yes`**：驅動程式驗證憑證鏈與主機名稱，信任錨由映像的系統憑證庫提供。

| | 做法 | 結論 |
|---|---|---|
| **採用：衍生映像層** | 共用的 `Dockerfile` **不動**。`build-image` 先建基底映像 `rac-app-base:<標籤>`，再以 `derived_dockerfile()` 產生的衍生 Dockerfile（`FROM` 基底、`USER root`、`COPY` 主機上的 `/etc/rac/rds-ca-bundle.pem`（在映像內命名為 `.crt`）到 `/usr/local/share/ca-certificates/`、`RUN update-ca-certificates`、`USER app`）建成 `rac-app:<標籤>`；兩個池、單元與所有一次性容器照舊用 `rac-app:<標籤>`，**容器沒有任何憑證掛載** | 主機上的 CA 套件（主機初始化 B 段取得並驗證）只是建置輸入 |
| 已否決：候選 A（`ServerCertificate`） | `Encrypt=yes` 加 `ServerCertificate=<CA 套件>` | 2026-11-17 在真實主機實測失敗（資料庫初始化的第一個連線即 `tls_certificate`、SQLSTATE 08001，尚未建立任何資料庫物件）。**原理性的不相容，不是設定錯**：ODBC Driver 18 的 `ServerCertificate`（18.1 起）是「伺服器憑證必須與檔案完全相同」的精確比對，取代標準驗證（到期、主機名稱、信任鏈），而且只在 `Encrypt=Strict`（TDS 8.0）時可用；RDS SQL Server 2019 不支援 strict，伺服器的葉憑證也會輪替，所以 CA 套件永遠比對不上。**連線字串、兩支腳本與探測都不得使用它**（探測拒絕含它的連線字串） |
| 已否決：執行時掛合併檔 | 把「Debian 憑證庫加 RDS CA」的合併檔唯讀掛到容器的 `ca-certificates.crt` | 實測可行，但合併檔是某一版基底映像憑證庫的快照，映像更新後會與新映像脫節，過期的根憑證可能讓模型供應商或 Google 的連線驗證失敗，每次部署都要重產 |
| **禁止** | `TrustServerCertificate=yes`、`SSL_CERT_FILE` 之類改變整個行程信任範圍的設定 | 會影響模型供應商、SMTP、Google 的 TLS 驗證；計畫與主機檔案的測試掃描它們不得出現 |

- **現場實測（2026-11-17，主機上以不存在的登入 `probe_nouser` 連 RDS：TLS 通過時只會在登入被拒，SQLSTATE 28000；沒通過則是憑證錯誤 08001）**：沒有 RDS CA 的現有映像 → 08001；`update-ca-certificates`，3 張憑證放同一個檔 → 28000；每張一個檔 → 28000；執行時掛合併檔 → 28000。採用單一檔（不需要拆檔）。
- **信任範圍**：變更只是在系統憑證庫**新增**這份區域 CA 套件的根憑證（只簽發 RDS 端點的憑證）；不關閉、不放寬任何驗證，原有的根憑證一張不少，所以對模型供應商、SMTP、Google 的 TLS 驗證結果不變。`check-image` 以 `grep -c 'BEGIN CERTIFICATE'` 數 `/etc/ssl/certs/ca-certificates.crt`，確認「部署用映像的憑證數＝基底映像的張數＋主機上套件的張數」，不符就以錯誤結束（標準輸出仍只是 `10001 clean`）；測試以 bash 執行這段腳本，涵蓋沒加進去、只加一張、多出不明憑證與少了原有憑證。
- **既有的 SSM `DATABASE_URL` 仍是舊型態**：`put-parameter-DATABASE_URL` 對已存在的參數會略過（不覆寫），所以新增 `refresh-parameter-DATABASE_URL`（`rds-ready` 組）：由執行器用 RDS 端點與 SSM 內的專屬登入密碼重新組成連線字串，經暫存檔覆寫；`Overwrite` 不能與標籤並用，所以不帶標籤（原有的保留）；輸出與命令列都沒有值，驗證只看名稱與型別；執行前一樣逐步確認。重複執行的結果相同。
- **標籤陷阱**：5.1 建出的 `rac-app:5681983` 沒有 RDS CA，**不可用於資料庫連線**（它的建置與非 root 檢查仍有效）；要資料庫連線的映像必須以新提交的短碼重跑 `build` 組。`--tag` 預設取目前 git 短提交（HEAD 會一直變），之後的 `migrate`、`restart` 等組要明確帶 `--tag`。
- **雲端驗證（task 5.3，2026-11-17，映像標籤 `48f5ab2`）**：①`build` 組兩段建置成功，`check-image` 通過：`10001 clean`，憑證庫 148 張（基底映像）＋3 張（RDS 套件）＝151 張，只新增、沒有減少；②`refresh-parameter-DATABASE_URL` 覆寫成功（覆寫後參數版本為 2、標籤保留，值不顯示；連線字串型態以探測的輸出確認）；③資料庫初始化在 RDS 上**第一個連線就通過憑證驗證**（候選 A 在這一步 `tls_certificate`），建立資料庫 `rac_demo`、登入 `rac_app`、資料庫使用者與 `db_owner`；第二次執行全部「已存在，略過」、不變更既有密碼；④探測成功：連線加密且伺服器憑證通過驗證，連線選項為 `Encrypt=yes`、沒有 `ServerCertificate`、沒有 `TrustServerCertificate`（採用的連線字串型態）；⑤主機沒有殘留：暫存目錄 0、`/run/rac` 空、容器 0、沒有 active 的 `rac-*` 單元。

### 資料庫初始化與連線探測（`scripts/aws_demo_bootstrap_db.py`、`scripts/aws_demo_probe_db.py`）

兩支腳本都在**主映像內以一次性容器執行**（design D7、D8；映像是含 RDS CA 的 `rac-app:<標籤>`）：映像不含 `scripts/`（`.dockerignore`），所以由主機把腳本唯讀掛入；各自獨立、不匯入其他維運腳本，所以可以單獨掛入（初始化不匯入 `app/`；探測只匯入應用程式的引擎工廠，容器的工作目錄是 `/app`，腳本由別處掛入時會把工作目錄加進匯入路徑）。它們不是執行器的子命令，而是 `deploy_plan` 的 `db-init` 組的步驟（task 5.3 在真實主機現場執行並逐項驗證後回寫）。順序固定為「建置映像（含 RDS CA）→ 覆寫 `DATABASE_URL` → 資料庫初始化 → 連線探測 → 快照與 migration」，探測沒有通過就**停止**，不進入 migration，也不改用放寬的設定。

`db-init` 組只在第一次部署以 `--group db-init` 執行，每個維運腳本的流程相同（`_ops_container_lines()`，兩段 SSM 命令各一段）：①本機 `upload-ops-…` 以 `--sse AES256` 把 `scripts/<腳本>` 傳到傳檔儲存貯體的 `ops/` 前綴（每次重傳，並驗證物件的加密）；②主機建立持久磁碟上的暫存目錄（`/var/tmp/rac-ops.*`，結束時一律刪除）、下載腳本、**比對計畫產生時算出的 SHA-256（不符就在取出任何機密之前失敗）**；③由機密取出腳本寫環境檔到記憶體檔案系統（初始化用 `bootstrap` 單元，只含 RDS 主要使用者與專屬登入的密碼；探測用 `app` 單元，與應用程式相同，**取不到主要使用者的密碼**；結束時一律刪除環境檔）；④用 `rac-app:<標籤>`（含 RDS CA）的一次性容器執行，強化選項與兩個池相同（唯讀根、丟掉所有 capability、不得提權、`--pull=never`），腳本唯讀掛入，**沒有任何憑證掛載**；初始化帶 `--host <RDS 端點>`（`deploy-find-db-endpoint` 以唯讀查詢取得）。容器的輸出（繁體中文的進度與失敗類別）送到該 SSM 命令的**標準錯誤**，標準輸出只有一行 `docker-exit=<代碼>`，等待步驟以 `docker-exit=0` 驗證；容器非零結束時整段腳本以同一個代碼結束，SSM 狀態為 Failed，執行器立即停止。測試以 bash 搭配假的 `aws`、`docker` 與機密取出腳本實際執行這兩段：成功、容器失敗（代碼原樣傳出）與腳本被竄改（不取機密、不啟動容器）三種情況，且暫存目錄與環境檔都已刪除。

| | `aws_demo_bootstrap_db.py`（初始化） | `aws_demo_probe_db.py`（探測） |
|---|---|---|
| 身分 | RDS 主要使用者 `racmaster`（密碼取環境變數 `RDS_MASTER_PASSWORD`） | 應用程式的專屬登入（環境變數 `DATABASE_URL`，與應用程式相同） |
| 輸入 | `--host`（RDS 端點，必填）；`--port`、`--master-user`、`--db-name`、`--app-login` 有預設值（與計畫模組的常數相同，測試比對；**沒有憑證相關的參數**）；應用程式登入的密碼取環境變數 `DB_APP_PASSWORD`；兩者來自機密取出腳本產生的 `/run/rac/bootstrap.env`（`--env-file`），**沒有任何密碼的命令列參數**，多給的參數一律拒絕且不回顯內容 | 沒有參數；連線只來自 `DATABASE_URL`，並經應用程式的引擎工廠 `create_engine_for` |
| 做什麼 | 依序「不存在才建立」：資料庫 `rac_demo` → 登入 `rac_app` → 資料庫使用者 → `db_owner` 角色（遷移要建表與改表；**沒有任何伺服器層級的角色**）。可重複執行：已存在就略過，**既有登入的密碼不重設**；資料庫內有同名但對應到別的登入的使用者時停止、不修改 | 以 ORM 表達式執行 `SELECT 1`，成功才允許 migration |
| 驗證憑證 | 只有 `Encrypt=yes`，由驅動程式以映像的系統憑證庫驗證憑證鏈與主機名稱；**沒有 `ServerCertificate`，也沒有任何信任所有憑證的選項** | 拒絕（不連線、結束代碼 2）：`TrustServerCertificate` 為真、`Encrypt` 關閉或 `optional`、不是 SQL Server 的連線字串、**含 `ServerCertificate`**（已否決的做法） |
| 輸出 | 名稱與「已建立／已存在」；失敗時只有步驟、類別、例外類型與 SQLSTATE | 成功或失敗與類別；成功時另印連線選項的型態（`Encrypt` 的值、沒有 `ServerCertificate`、沒有 `TrustServerCertificate`），供記錄採用的連線字串型態（task 5.3 已記錄：`Encrypt=yes`） |
| 結束代碼 | 0 成功；1 資料庫操作失敗；2 輸入不合法或缺少環境變數（沒有連線） | 0 成功；1 連線或查詢失敗；2 設定不合格（沒有連線） |

- **名稱與密碼的白名單**：資料庫、登入與主要使用者名稱只含英數與底線（英文字母開頭、最長 63 字），不得以 `db_`、`sys`、`rds` 開頭，不得是系統資料庫或 `sa`、`dbo`、`public`、`guest`；應用程式登入不得與主要使用者相同；密碼 12 至 128 字且只含英數、`_` 與 `-`（與執行器產生的密碼同一字元集，測試以 200 次產生確認全部通過），兩個密碼不得相同。DDL 無法使用參數，所以識別字經白名單與方括號、密碼值以字元集排除引號；存在性查詢一律以參數傳入。語句只有 `CREATE DATABASE`、`CREATE LOGIN`、`CREATE USER`、`ALTER ROLE ... ADD MEMBER` 四種，屬維運腳本的 T-SQL（位於 `scripts/`，不在 `tests/architecture/test_no_raw_sql.py` 掃描的 `app/` 與 `migrations/`，與 `scripts/restore_drill.py` 的 `RESTORE` 同類）。
- **機密不外洩**：密碼只經環境變數進入行程，不寫入日誌、輸出、命令列或例外訊息；失敗時**不輸出例外訊息**（驅動程式的訊息可能含伺服器位址或登入名稱），只輸出固定的類別。類別為 `login_failed`、`tls_certificate`（憑證無法驗證：停止並回報，不放寬）、`timeout`、`network`、`driver_missing`、`password_policy`、`permission_denied`、`orphaned_user`、`unknown`。驅動程式的訊息會依作業系統語系在地化（繁體中文 Windows 上的憑證失敗是中文），所以分類另比對不分語系的標記與 Windows 的錯誤碼；兩支腳本各自帶一份相同的規則，測試比對。
- **已驗證**：名稱與密碼的白名單、四個語句的內容與順序、重複執行不再執行任何 DDL 且不重設密碼、中途失敗即停止並指出步驟、輸出不含密碼與位址、分類與命令列的拒絕行為，由 `tests/test_aws_demo_bootstrap_db.py` 與 `tests/test_aws_demo_probe_db.py` 驗證；另有標記 `sqlserver` 的整合測試對 `TEST_SQLSERVER_URL` 的本機 SQL Server 實際執行兩次初始化（登入可連線、是 `db_owner`、不是 `sysadmin`、可建表、第二次不重設密碼；測試自己建立並清除 `racbootit_*` 的資料庫與登入，連不到時標為跳過），以及取得真實的驅動程式錯誤（未信任的自簽憑證歸為 `tls_certificate`、錯誤密碼歸為 `login_failed` 且 SQLSTATE 為 28000）。
- **已在真實 RDS 驗證（task 5.3，2026-11-17）**：主要使用者 `racmaster` 在 RDS 上可以 `CREATE DATABASE`、`CREATE LOGIN`（`CHECK_POLICY = ON` 的密碼規則接受執行器產生的密碼）、`CREATE USER` 與 `ALTER ROLE db_owner ADD MEMBER`；第二次執行全部略過、不重設密碼；應用程式登入以 `Encrypt=yes` 連線並由映像的系統憑證庫驗證憑證（探測成功）；結果見上一節的「雲端驗證」。
- **未驗證**：英文（Linux 容器）的驅動程式訊息只以樣本字串驗證，沒有取得真實的失敗訊息（5.3 的連線全部成功）；`db_owner` 之外的權限邊界（例如應用程式登入是否真的不能碰其他資料庫）只由角色成員的設計保證，沒有在 RDS 上逐項嘗試。探測或初始化失敗（`tls_certificate` 或其他）時：停止並回報，**不**採用 `TrustServerCertificate=yes`、`ServerCertificate` 或行程範圍的 `SSL_CERT_FILE`。

**遷移與種子（task 5.4，2026-11-17，映像 `rac-app:48f5ab2`；`deploy --group snapshot --group migrate --tag 48f5ab2`，沒有 restart）**：①`snapshot-db` 建立手動快照 `rac-demo-staging-db-pre-48f5ab2`（`manual`、加密、20 GB、帶 `Project`／`Environment`／`Name` 標籤），等到 `available` 才遷移；②`migrate-db` 在空資料庫上依序執行 `0001_baseline` 到 `0018_rpg_turn_revision` 共 18 個遷移（MSSQLImpl，交易式 DDL），結束代碼 0；③`check-migration-head` 輸出 `1`，另以唯讀的一次性容器直接查 `alembic current`＝`0018_rpg_turn_revision (head)`；④`seed-scripts`：建立 1 份內建劇本（`xianxia-qingyun`）、更新 0、沒有變動 0；⑤**沒有啟動或重啟任何服務**（兩個池與 Caddy 仍未啟動，網址仍連不上是正常的）。手動快照不隨 RDS 消失、持續計費，拆除時依 design D14 處理（部署前快照預設刪除、由使用者指定例外）。**觀察**：`migrate-db`、`check-migration-head`、`seed-scripts` 沿用 `_one_off_lines`，執行後 `/run/rac/app.env`（記憶體檔案系統、600、僅 root）仍留著，與 `db-init` 的腳本（結束時刪除環境檔）不一致；兩個池的單元每次啟動都會重新產生它，所以不影響功能。**已決定不清除**（理由見下方「啟動兩個池」一節的 `/run/rac/app.env` 條目）。

### 建立首位管理員（task 5.5，`create-admin` 組）

規格要求密碼由使用者在自己的終端機輸入、不經腳本參數與對話，所以這一步是**人工步驟**（`Step.interactive`，`argv` 為空）：執行器只把要輸入的指令列出來（`Step.manual_commands`，**只顯示、不執行**），等你回答「做完了」才繼續；`--yes` 不能代替這個確認。整組（`deploy --group create-admin --tag <標籤> --execute`，在你自己的終端機執行，因為要回答確認）依序是：

1. **乾跑**（`check-create-admin`＋等待）：主機上以與人工步驟相同的映像、強化選項與環境檔執行 `flask --app wsgi create-admin --help`（只印說明，不詢問、不寫入資料庫），證明映像、環境檔、`flask` 與 `wsgi` 模組都可用；輸出一行 `create-admin-help=ok`，環境檔在結束時（含失敗）一律刪除。
2. **人工步驟**（`create-admin`）：在 AWS 主控台開啟主機的 Session Manager 終端機（登入使用者 `ssm-user`，要 `sudo`；主機只開放 SSM，沒有 22 與金鑰），依序輸入兩行：`sudo /usr/local/sbin/rac-render-env-app`（把機密取到記憶體檔案系統的環境檔），再 `sudo /usr/bin/docker run --rm -it --name=rac-create-admin --pull=never …（與兩個池相同的強化選項）rac-app:<標籤> flask --app wsgi create-admin`。**指令之後沒有任何參數**：信箱、顯示名稱由指令詢問，密碼以不回顯的方式輸入並再輸入一次確認；成功時印出「已建立管理員帳號（識別碼 …）」。
3. **清理**（`cleanup-create-admin`＋等待）：刪除互動容器與 `/run/rac/app.env`（使用者中途關閉終端機可能殘留），輸出 `app-env-left=0 container-left=0`，不符就失敗。清理由計畫做，不要求使用者手動刪檔。

- **為什麼要有乾跑與清理**：人工指令是使用者自己輸入的，輸入的是真的會寫入資料庫的指令，所以先證明它用的映像與環境檔可用；而取出機密的環境檔不能留在記憶體檔案系統上（與 `db-init` 的腳本一致），不能指望人記得刪。
- **機密與帳密不經過的地方**：指令列、計畫步驟、執行器輸出、SSM 命令（兩個 SSM 腳本都沒有 `-it`、沒有帳密）與對話；測試掃描人工指令不得出現 `--email`、`--display-name`、`--password`、`${`、`{{` 與 `rm -f`。
- **只在第一次部署**：`FIRST_DEPLOY_GROUPS` 含 `create-admin`，例行部署的整份計畫不含它。順序在遷移之後（需要資料表）、`restart` 之前。指令本身（`app/cli.py`）對**已存在的信箱**拒絕且不改動既有帳號（`email_exists`），所以同一個信箱重做是安全的；它不限制管理員的數量，是否已建立要由部署者自己掌握。
- **信箱要填之後用來 Google 登入的那一個**：管理員每次登入都先過 Google，第一次以「同信箱且 `email_verified`」的 Google 身分登入時才綁定，之後再驗證一次這裡設定的密碼（密碼 8 至 128 字）；信箱填錯就綁不上。
- **已驗證**：`tests/test_aws_demo_plan.py` 與 `tests/test_aws_demo_script.py` 驗證順序、互動旗標、人工指令的內容（沒有任何帳密參數）、兩個 SSM 腳本在 bash 內以假的 `docker` 與機密取出腳本實際執行（成功、容器失敗、環境檔與容器清除）、預覽不呼叫任何外部指令、`--yes` 不能代替人工確認。**雲端**：乾跑已在真實主機以映像 `rac-app:48f5ab2` 成功（SSM `Success`，輸出 `create-admin-help=ok`）。
- **雲端人工步驟，第一次（2026-11-17）——其實沒有建成**：當時使用者回報在 Session Manager 終端機完成並回答「做完了」，接著的 `cleanup-create-admin` 驗證通過（`app-env-left=0 container-left=0`），整組以「全部完成」結束，task 5.5 因而勾選。**第 7 批的 8.2 發現資料庫裡沒有任何管理員**：使用者編號 1 至 9 全都對得上其他帳號（兩位 6.4 測試玩家、五個 7.3 告警演練帳號、8.1 的測試玩家、8.2 第一次 Google 登入註冊的玩家），沒有任何編號給過管理員。**原因沒有查明**；第二次嘗試時使用者回報「本機執行器只問 `[y/N]`，全按 y 就做完了」，與資料庫沒有任何變化一致：**執行器只列出要輸入的指令並問「做完了嗎？」，信箱、顯示名稱與密碼的提示出現在 Session Manager 終端機，不在執行器裡**，所以只在本機回答 `y` 會得到「全部完成」，卻什麼都沒建立。
- **雲端人工步驟，第二次（2026-11-17，約 06:57 UTC）——建成並驗證**：使用者在 Session Manager 終端機（EC2 主控台的「連線」要選 **SSM Session Manager** 那一項，不是預設的 EC2 Instance Connect，這台主機沒有開 SSH）貼上兩行指令，回答三個提示，看到「已建立管理員帳號」。我以唯讀的一次性程式（容器內、只走 ORM、遮蔽信箱）確認：`users` 只有編號 10，角色 `admin`、狀態 active、信箱已驗證、有密碼、當時沒有 Google 身分；互動容器沒有殘留，兩個池仍在執行。其後管理員 Google 登入成功（見「Google 登入人工驗收」）。這次沒有重跑 `cleanup-create-admin`：`/run/rac/app.env` 在兩個池啟動後本來就留著（已決定不清除），互動容器是 `--rm`。
- **教訓與待決定的改善**：①人工步驟的成功**不能只靠使用者在本機回答 `y`**；②這一組沒有任何步驟驗證管理員存在（原本想的唯讀查詢因為沒有現成指令而不做，結果漏掉了這個失敗）。**待使用者決定**（程式與測試變更，超出 8.x 範圍）：把執行器的提示改成明確寫出「請先到 AWS 主控台的 Session Manager 終端機完成上述步驟」，並在 `cleanup-create-admin` 之後加一個唯讀的「確認管理員存在」步驟（例如在一次性容器內以 ORM 只印管理員人數）。

### 啟動兩個池（task 6.1，`restart`、`verify` 組）

**執行（2026-11-17）**：`deploy --account-id 179601693865 --group restart --group verify --tag 48f5ab2 --execute`（`--tag` 明確帶主機上的映像標籤 `48f5ab2`，不是新的 HEAD）。5 步全部成功：找出主機 → `switch-release`（`/etc/rac/release.env` 換成 `RAC_IMAGE_TAG=48f5ab2`，重啟 `rac-cs.service` 與 `rac-rpg.service`）→ 等待 `Success` → `check-pools`（兩個池在回送位址 8081、8082 的 `/readyz` 回 200，輸出 `rac-app:48f5ab2 rac-app:48f5ab2`）→ 等待 `Success`。這是兩個池第一次啟動，沒有前一版，所以沒有產生 `release.previous.env`，`rollback` 也沒有演練。

**唯讀查證**（另以 SSM 送出只讀指令，只輸出被查的欄位，不印環境變數與任何機密；現場版的指令已回寫成 `verify` 組的 `check-pool-hardening` 步驟，見下方「回寫成計畫步驟」）：

| 項目 | 結果 |
|---|---|
| 非 root | 兩個容器的 `Config.User` 都是 `app`；`docker top` 的行程 UID 都是 10001；容器內 `id` 為 `uid=10001(app) gid=10001(app)`；根檔案系統唯讀（`ReadonlyRootfs=true`） |
| 各用自己的 gunicorn 設定 | 客服池的行程指令是 `gunicorn --config gunicorn.conf.py wsgi:app`，RPG 池是 `gunicorn --config gunicorn.rpg.conf.py wsgi:app`；各有 3 個 gunicorn 行程（1 個主行程加 2 個 worker，與設定檔的 `workers` 預設 2 相符） |
| 只綁回送位址 | `docker port`：客服池 `8080/tcp -> 127.0.0.1:8081`、RPG 池 `8080/tcp -> 127.0.0.1:8082`；主機上的 TCP 監聽只有 `127.0.0.1:8081`、`127.0.0.1:8082`（兩個 `docker-proxy`）與 `127.0.0.1:41519`（`containerd` 自己的回送埠），沒有任何監聽在 `0.0.0.0` 或外部位址。**這只證明主機層**；安全群組是否擋住外部直連由 6.3 驗證 |
| 穩定性 | 兩個容器 `running`、`RestartCount=0`，單元 `NRestarts=0`；Caddy 與五個排程計時器都是 `inactive`（符合預期，6.2、7.1 才啟動） |

**記憶體（啟動後約 50 秒與約 90 秒各量一次；沒有任何流量）**：

| | 客服池 `rac-cs` | RPG 池 `rac-rpg` |
|---|---|---|
| `docker stats`（cgroup 用量） | 92.95 MiB → 93.16 MiB（4.88%） | 92.48 MiB → 92.36 MiB（4.84%） |
| 容器記憶體上限 | 無（`HostConfig.Memory=0`，百分比的分母是主機總量 1.865 GiB） | 無 |

主機 `free -m`（第二次）：總量 1909、已用 508、空閒 85、緩衝與快取 1316、可用 1225（單位 MB）；交換空間 2047 MB，只用了 40 KB。兩次量測相差不到 1 MiB，沒有看到持續增長。

- **讀數的限度**：①`docker top` 三個行程的 RSS 合計約 248 MiB，大於 `docker stats` 的 93 MiB，因為主行程與 worker 共用分頁，RSS 會重複計算；以 `docker stats` 為準。②systemd 單元的 `MemoryCurrent`（約 8 MB）判斷只計 `docker run` 的用戶端行程，不含容器（容器在 docker 自己的 cgroup 內），不能當作池的記憶體。③這是**閒置**讀數：有流量（尤其是 RPG 串流與兩個池同時忙碌）時的記憶體屬 `rpg-ops-loadtest` 的量測，展示環境不是容量依據。
- **`/run/rac/app.env` 仍留著，已決定不清除**（2026-11-17，使用者採納建議）：池與排程工作的單元每次啟動都會重新產生它，所以池啟動之後它還在（證實了 5.4 的觀察）。理由：①**查證**（唯讀）：`/run` 是 tmpfs，`/run/rac` 權限 700、檔案 600，擁有者都是 root，docker 群組沒有成員，所以能讀它的只有 root；root 本來就能用 `docker inspect`、`/proc/<pid>/environ` 或實例角色讀到同樣的機密，這個檔沒有讓更多人讀到機密；②規格要求的是「記憶體檔案系統、僅擁有者可讀、不寫入持久磁碟、映像、日誌或命令列」，沒有要求用完即刪，現況符合；檔案不含 RDS 主要使用者密碼（那個只在 `db-init` 的暫存檔，用完即刪）；③**清掉不安全**（從程式推論，**沒有在主機上實測**）：兩個池與五個排程工作共用同一個路徑，寫入是先寫暫存檔再改名（安全），但「啟動後刪除」會與同時啟動的另一個單元競爭（`systemctl restart rac-cs rac-rpg` 同時啟動兩個池；`check-alerts` 每 5 分鐘一次），對方的 `docker run --env-file` 可能讀不到檔案；要做對得讓每個單元用自己的檔案路徑，連帶改機密取出腳本、單元、一次性容器與測試，換到的只是「只有 root 讀得到的檔案少存在幾分鐘」，而排程工作每 5 分鐘就會重新產生它；④這個環境只用到 2026-12-31，正式環境（Cloud Run 以 Secret Manager 注入）沒有這個檔。**維持的不一致**：`db-init`（它的暫存環境檔含 RDS 主要使用者密碼，必須刪）與 `create-admin`（只在第一次部署、池還沒啟動時執行，人工步驟可能中途關閉終端機而殘留，所以清理順便刪檔）用完會刪，`migrate-db`、`seed-scripts` 與池、排程工作用同一份 `app.env`，不刪（刪了有上述競爭）。**何時重新考慮**：若日後要求機密「只在需要的瞬間存在」，改為每個單元獨立的檔案路徑再加啟動後刪除，不要只在某個單元加刪除。
- **未驗證**：外部是否連得到兩個池（Caddy 尚未啟動，6.2 與 6.3）、有流量時的記憶體與延遲、`rollback`。

**回寫成計畫步驟（`verify` 組的 `check-pool-hardening`，部署與回復都有）**：把上表前三列做成一個唯讀的 SSM 步驟，之後每次例行部署與回復都會驗。它**只看兩個池自己的埠**，不檢查主機的全部監聽，因為 Caddy 啟動之後（6.2）主機本來就會監聽 80 與 443，檢查全部會誤判。

- **檢查什麼**（對每個池）：①`docker top <容器> -eo pid,uid,args` 列出的所有行程都以 UID 10001 執行；②行程指令只有 `… gunicorn --config <該池的設定檔> wsgi:app` 一種（客服池 `gunicorn.conf.py`、RPG 池 `gunicorn.rpg.conf.py`）；③`docker port` 只有 `8080/tcp -> 127.0.0.1:<埠>`，且 `ss -ltnH 'sport = :<埠>'` 的本地位址只有 `127.0.0.1:<埠>`（沒有 `0.0.0.0`、`*`、IPv6，也不是沒在監聽）。
- **輸出**：全部符合才輸出固定的一行（`rac-cs uid=10001 config=gunicorn.conf.py bind=127.0.0.1:8081 rac-rpg uid=10001 config=gunicorn.rpg.conf.py bind=127.0.0.1:8082`），等待步驟以它驗證；任何一項不符就在標準錯誤印出被比對的值（UID、gunicorn 的行程指令、埠對應，沒有機密）並以 1 結束，SSM 狀態為 `Failed`，等待步驟立即停止。腳本只用 `docker top` 與 `docker port`（不 exec、不 inspect、不印環境變數）與 `ss`，不改動任何東西。
- **真實主機上發現的差異（2026-11-17）**：第一版腳本用 `docker top <容器> -eo uid`，在真實主機上被 daemon 拒絕（`Couldn't find PID field in ps output`，`-o` 清單一定要含 `pid`），步驟失敗並停止；改為一次 `-eo pid,uid,args` 後通過。**原本的測試沒有抓到**，因為假的 `docker` 接受任何參數；現在假的 `docker top` 跟真的一樣，缺 `pid` 就回同一個錯誤，並另有測試鎖住 `-eo` 清單的第一欄是 `pid`。
- **已驗證**：`tests/test_aws_demo_plan.py` 在 bash 內以假的 `docker top`、`docker port` 與 `ss` 實際執行：兩個池都符合時輸出固定一行；14 種不符（一個 worker 以 root 執行、整個池以 root 執行、兩個池互換設定、多出 `--bind` 參數、多出別的行程、容器埠發布在所有介面或多一個 IPv6、發布到別的主機埠、主機上監聽在 `0.0.0.0`、`*`、IPv6 或根本沒在監聽、完全沒有行程）都失敗且沒有固定一行；`docker` 本身失敗也失敗。**15 項破壞驗證全部被抓到**（含原始的 `-eo uid`）。**雲端**：對真實主機執行 `deploy --group verify --tag 48f5ab2 --execute`，`check-pools` 與 `check-pool-hardening` 都 `Success` 且輸出符合（上一段的第一版失敗過一次，是修正之前的紀錄）。這一步是唯讀的，沒有重啟任何服務。

### 啟動入口與排程（task 6.2、7.1 都已對真實主機驗證，`ingress`、`schedule` 組）

**狀態：`ingress` 組已於 2026-11-17（第 3 批）對真實主機執行並驗證，第一次執行就通過、沒有需要回寫的差異（task 6.2）；`schedule` 組已於 2026-11-17（第 5 批）對真實主機執行並驗證，同樣第一次執行就通過、沒有需要回寫的差異（task 7.1）**。兩組都只在第一次部署以 `--group` 明確執行，例行部署的整份計畫不含它們：例行部署只重啟兩個池，入口與計時器一直在跑。入口的設定變更也用 `--group ingress` 套用，因為 Caddy 的管理介面是關閉的（`admin off`），沒有熱重載，只能重啟。兩組都排在 `verify` 之後：兩個池先回 200 才啟動入口。

**`ingress` 組**（`start-ingress`、`check-ingress`，各接一個等待步驟）：

- **`start-ingress`**：①`docker pull --quiet caddy:2`（映像拉不到就停在重啟之前，執行中的入口不受影響；匿名拉取是否會被 Docker Hub 限流**沒有實測**）；②`systemctl restart rac-caddy.service`；③最多等 30 秒到容器進入執行狀態（單元是 `Type=simple`，`restart` 在行程建立時就回來，所以要自己確認容器真的在跑）。輸出 `ingress=running`。
- **`check-ingress`**：在主機內用 `curl --resolve <網域>:<埠>:127.0.0.1` 連回送位址，**仍然經過 Caddy**，而且網域與 TLS 的 SNI 都是展示網域，所以憑證必須真的簽給它、被主機的系統憑證庫信任（沒有 `-k`、`--cacert`）。不依賴「主機連自己的 Elastic IP 是否通」（沒驗證過，而且那條路徑不屬於 Caddy 的行為）。依序驗證：①`https://<網域>/healthz` 在最多 150 秒內（每 5 秒試一次，等憑證簽發）回 200，且 `http_version` 是 2、`ssl_verify_result` 是 0；②`http://<網域>/healthz` 回 301、302、307 或 308，且導向 `https://<網域>/healthz`（Caddy 實際用哪個狀態碼由雲端執行記錄，這裡不先猜）；③回應標頭含 HSTS（`max-age=31536000`）與四個安全標頭（`X-Content-Type-Options`、`Referrer-Policy`、`X-Frame-Options`、`Permissions-Policy`；名稱與值取自 `app/security/security_headers.py`，不另寫一份；CSP 刻意不送，所以不要求）。比對不分大小寫（HTTP/2 的標頭名稱是小寫）、整行比對（值多出文字不算符合），缺哪個就印哪個。全部符合才輸出 `ingress tls=ok http=2 redirect=https headers=5`；憑證的簽發者與到期日印在標準錯誤供記錄，不參與驗證。
- **真實主機的結果（2026-11-17，`deploy --group ingress --tag 48f5ab2 --execute`）**：`docker pull caddy:2` 成功（匿名拉取沒有被限流）；容器（`--read-only`、`--tmpfs=/config`、`admin off`、`--cap-drop=ALL` 加 `NET_BIND_SERVICE`）正常啟動，輸出 `ingress=running`；`check-ingress` 輸出 `ingress tls=ok http=2 redirect=https headers=5`。憑證由 Let's Encrypt 簽發（`CN=YE2`，到期 2027-02-15），**啟動後約 15 秒完成**（`start-ingress` 04:41:13 與 `check-ingress` 04:41:28 UTC；150 秒的上限綽綽有餘）；HTTP 導向用 **308**。另從使用者的電腦對公開網域做了唯讀驗證（不經主機）：ALPN 協商 `h2`、TLS 1.3、系統憑證庫驗證通過、HTTP 回 308 導向 HTTPS、`/healthz` 與 `/readyz` 本文只有 `{"status":"ok"}`。**觀察**：回應帶 `Alt-Svc: h3=":443"`，但安全群組沒有開 UDP 443，HTTP/3 用不到、瀏覽器會退回 HTTP/2；沒有修改（關閉 HTTP/3 或開 UDP 443 由使用者決定）。
- **不在這一組驗**：分流與暫停客服池（`routing` 組，見下一節）、外部直連應用程式埠被拒（同一節）；串流有沒有被緩衝（6.4，見「串流探測」）。

**`schedule` 組**（`start-timers`、`run-check-alerts`、`check-schedule`，各接一個等待步驟）：

- **`start-timers`**：`systemctl start` 五個計時器（主機初始化只 `enable`，沒有啟動）；輸出作用中的計時器數 `timers-active=5`。計時器是 `Persistent=true`，但第一次啟動沒有上次觸發的紀錄，預期不會補跑（雲端執行時確認）。
- **`run-check-alerts`**：`systemctl start rac-job-check-alerts.service` 並等它結束。它與計時器觸發的是同一個服務單元（同映像、同環境檔、同強化），所以這次執行也證明了排程工作的容器能跑；它是真正執行的 `check-alerts`，有告警條件且設有收件人時會寄信（空的展示資料庫沒有）。失敗時把該單元最後 20 行日誌印到標準錯誤（日誌依規範不含機密與個資）並以 1 結束。成功輸出 `check-alerts result=success status=0`（`systemctl show -P Result` 與 `ExecMainStatus`）。
- **`check-schedule`**：唯讀。①作用中的 `rac-job-*.timer` **恰好等於**由 `SCHEDULED_COMMANDS` 產生的五個（排序用 `LC_ALL=C`，與 Python 的排序一致，不受主機語系影響）；②每個計時器都有下一次觸發時間（`NextElapseUSecRealtime` 不是空的也不是 `n/a`）；③`systemctl --failed` 沒有任何失敗單元（整台主機，不只 `rac-*`，不符時印出單元名稱）。輸出 `timers=5 next=5 failed=0`。計時器清單來自 `SCHEDULED_COMMANDS`：新增排程指令時，步驟內的單元與預期的數字（`timers-active=N` 等）跟著變，測試用暫時加入的指令鎖住這一點。
- **不在這一組驗**：計時器實際在預定時間觸發並成功（見下方「真實主機的結果」：只有每 5 分鐘的 `check-alerts` 看到自行觸發）；`OnCalendar` 的時間點本身（單元檔內容在 `host-init` 的 `files` 步驟以 SHA-256 驗過；`systemctl` 會把 `*:0/5` 正規化成 `*:00/5`，所以不拿字串比對它）。
- **真實主機的結果（2026-11-17，`deploy --group schedule --tag 48f5ab2 --execute`，第 5 批）**：7 步全部達成、結束代碼 0，輸出依序是 `timers-active=5`、`check-alerts result=success status=0`、`timers=5 next=5 failed=0`。**沒有需要回寫的差異**：計時器第一次啟動沒有補跑（`Persistent=yes`，但沒有上次觸發的紀錄）。另做了獨立的唯讀 SSM 查證：`systemctl list-timers 'rac-job-*'` 列出恰好五個（`check-alerts` 下次 05:25:00 UTC、`purge-unconfirmed-accounts` 06:00:00、`purge-inactive-accounts` 19:00:00、`execute-account-deletions` 19:20:00、`purge-expired-data` 19:40:00，全是 UTC，等於台灣時間凌晨 03:00 到 03:40 的三個每日工作加每小時與每 5 分鐘兩個）；`systemctl show -P NextElapseUSecRealtime` 與 `Persistent` 可用（`next=[Tue 2026-11-17 05:25:00 UTC] persistent=[yes]`）；`journalctl -o cat` 可用；`systemctl --failed` 本來就是空的。手動跑的 `check-alerts` 完整跑完並寫出心跳 `alert.check_completed`，五項評估為「正常」或「樣本不足」。**觀察（沒改）**：排程容器每次啟動，日誌都有一行 `Error response from daemon: No such container: rac-job-check-alerts`，來自單元的 `ExecStartPre=-docker rm -f`（`-` 前綴讓失敗被忽略，docker 仍把錯誤印出來），無害。**證據限度**：只有 `check-alerts` 的每 5 分鐘計時器在這次對話中看到自行觸發（05:25、05:30、05:35 UTC 都有它的日誌）；其餘四個每小時或每日的計時器還沒等到觸發時間，所以它們實際觸發並成功沒有驗證。

**假指令與已驗證**：兩組的主機腳本在 bash 內以假的 `curl`、`openssl`、`docker`、`systemctl`、`journalctl` 實際執行。**假指令跟真的一樣嚴格**（`check-pool-hardening` 的教訓）：不認得的選項或子指令就失敗；假 `curl` 還要求每次請求都帶與網址的主機和埠相符的 `--resolve`（沒帶就像真的 curl 去解析 DNS 一樣失敗）、不接受 `-k`、`-w` 的格式只認得計畫用的兩種；假 `systemctl` 只認得計畫用的子指令與完全相同的引數。涵蓋的情境：成功、等憑證簽發（前幾次失敗後成功）與 30 次都失敗、HTTP/1.1、驗證結果非 0、502、四種導向狀態碼與不導向／導向到 http／別的網域／別的路徑、五個標頭各缺一個、HSTS 值不對、值多出文字、HTTP/2 小寫與 HTTP/1.1 原樣標頭、容器一直不跑、拉映像失敗（不碰執行中的入口）、重啟失敗、手動跑 `check-alerts` 失敗（印日誌、沒有成功行）、少一個計時器、多一個計時器、沒有下一次觸發、`n/a`、失敗單元（含不屬於 `rac-*` 的）、計時器列表亂序。**34 項破壞驗證全部被抓到**（改壞腳本後測試會失敗：不拉映像、先重啟再拉、改成 `systemctl start`、不等容器、容器沒跑也不失敗、HTTPS 加 `-k`、拿掉 `-f`、三個 `curl` 各拿掉 `--resolve`、不要求 HTTP/2、不看憑證驗證結果、重試只一次、不驗導向位置、303 也算導向、標頭比對改成子字串、標頭不轉小寫、少要求 HSTS、缺標頭不失敗、憑證資訊不印、少啟動一個計時器、失敗時不印日誌或不停止、排序沒有 `LC_ALL=C`、核對時不看失敗單元／下一次觸發／清單、`list-units` 拿掉 `--state=active`、兩組的順序與歸屬、單選時不帶找主機的步驟、兩個等待步驟的預期值放寬；其中兩項一開始看似沒被抓到，是我的測試篩選字串漏選了對應的測試，改用較寬的篩選後都被抓到）

**已在真實主機驗證（`ingress` 組，2026-11-17）**：`docker pull caddy:2` 的行為、`openssl s_client` 在 AL2023 上取得憑證資訊（簽發者與到期日有印在標準錯誤）、Caddy 首次簽發憑證的時間（約 15 秒）、Caddy 的導向狀態碼（308）、`curl` 在主機上協商到 HTTP/2。

**已在真實主機驗證（`schedule` 組，2026-11-17）**：`systemctl show -P`（Amazon Linux 2023）與 `NextElapseUSecRealtime` 的輸出格式、`journalctl -o cat`、`systemctl --failed` 在真實主機本來就是空的、排程單元 `rac-job-check-alerts.service` 的容器能在主機上跑完並成功結束。

**未驗證**：每小時與每日的四個計時器（`purge-unconfirmed-accounts`、`purge-inactive-accounts`、`execute-account-deletions`、`purge-expired-data`）實際在預定時間觸發並成功；`Persistent=true` 的補跑行為（主機停機再開機後）。

### 驗證分流（task 6.3，`routing` 組）

**狀態：已於 2026-11-17（第 3 批）對真實主機執行並驗證，第一次執行就通過、沒有需要回寫的差異。** 只在第一次部署以 `--group routing` 明確執行（例行部署與回復都不含：它會暫停客服池，客服在暫停期間不可用）；排在 `ingress` 之後，因為要 Caddy 起來才有東西可以驗。

**為什麼要暫停客服池**：兩個池跑的是同一份應用程式（只差 gunicorn 設定），任何一個都能回應任何路徑，所以回應本身分不出「是誰處理的」。唯一站得住腳的依據是讓其中一個池沒人在聽：被暫停的客服池沒有人在聽，入口導向它的請求只能得到 Caddy 自己的 502；導向 RPG 池的請求照常得到應用程式的回應。兩個方向都在同一次暫停裡看見，就證明了 Caddyfile 的分流（`/api/game/*` 到 RPG 池、其餘到客服池）。使用者已同意暫停客服池（驗完立刻恢復）。

- **`check-routing`**（後接一個等待步驟）：①兩個池在回送位址上 `/healthz` 都回 200；②暫停前，入口（`curl --resolve 網域:443:127.0.0.1`，仍經 Caddy）的三條路徑都有正常的應用程式回應：`/healthz` 200，`/api/cs/conversations` 與 `/api/game/sessions/1` 未登入 401（只用 GET、不需要 cookie、不碰資料庫）；③`systemctl stop rac-cs.service`；④暫停中：`/healthz` 與 `/api/cs/conversations` 回 502（客服池處理）、`/api/game/sessions/1` 仍回 401（RPG 池處理）、RPG 池回送位址的 `/healthz` 回 200、客服池的回送位址連不上；⑤`systemctl start rac-cs.service`，等客服池 `/readyz` 回 200（每 5 秒一次，最多 150 秒）；⑥恢復後三條路徑回到暫停前的狀態碼。全部符合才輸出 `routing game=rpg other=cs paused=502 rpg=200 restored=ok`。
- **一定恢復**：腳本用 `trap` 在結束時把客服池啟動起來——任何一項驗證失敗、`systemctl stop` 半途失敗、`systemctl start` 失敗（會再試一次並印出訊息）、收到 `HUP`、`INT`、`TERM` 都一樣；`restored` 在 `systemctl stop` 之前就設為 0。**限制**：如果 SSM 命令被取消或逾時，AWS 怎麼終止主機上的程序我沒有驗證（不確定 `trap` 會不會執行）；發生時用 `deploy --group restart` 或 `systemctl start rac-cs.service` 恢復，`deploy --group verify` 可確認兩個池都在。
- **真實主機的結果**：SSM 命令約 40 秒，輸出 `routing game=rpg other=cs paused=502 rpg=200 restored=ok`，標準錯誤一行 `curl: (7) Failed to connect to 127.0.0.1:8081`（推斷是恢復後第一次 `/readyz` 探測時客服池還沒開始監聽，等 5 秒重試成功；客服不可用的時間因此不超過 40 秒）。之後獨立複驗：從使用者的電腦對公開網域再測 `/healthz` 200、`/api/cs/conversations` 401、`/api/game/sessions/1` 401；唯讀的 `deploy --group verify` 通過（兩個池的 `/readyz` 200、`check-pool-hardening` 符合）。
- **外部直連應用程式埠被拒（不是計畫步驟，因為必須從主機外部測）**：從使用者的電腦以 TCP 連線連 Elastic IP（6 秒逾時）：443、80 連得上（0.2 秒）；**8081、8082、22、1433 都逾時**（安全群組丟棄封包，不是明確拒絕）。對照安全群組唯讀查詢：主機群組只有 TCP 80 與 443 來自 `0.0.0.0/0`，資料庫群組只有 1433 來自主機群組。證據的限度：只從一個來源測；主機內部的監聽只有 `127.0.0.1`（`check-pool-hardening` 已驗證）。
- **已驗證（本機）**：`tests/test_aws_demo_plan.py` 在 bash 內以**嚴格的**假 `curl`（只接受計畫用的兩種呼叫形式、入口請求要求與網址相符的 `--resolve`、直連要求 `-f` 與 5 秒逾時、不認得的選項就失敗）與假 `systemctl`（只認得 `stop`／`start rac-cs.service`）實際執行腳本：成功的完整呼叫順序與只暫停一次；等就緒（重試 3 次後成功、30 次都失敗則回報且不驗恢復後的路徑）；暫停前狀態不對（三條路徑的狀態碼、兩個池的健康）時完全不暫停；分流錯誤（兩池互換、全部導向 RPG 池、全部導向客服池）、停止沒有生效、RPG 池在暫停中不健康、客服池在暫停中仍回應回送位址，都失敗並仍恢復；`stop` 失敗、`start` 失敗（trap 再試一次並回報）、暫停中收到 `TERM` 都恢復；腳本只有 `systemctl stop|start rac-cs.service`、只送匿名 GET、沒有 `-k`、cookie 或任何對其他單元、容器、機密檔的操作；三條探測路徑與 Caddyfile 的分流前綴一致；命令逾時涵蓋最壞情況（約 460 秒，上限 600 秒）；`tests/test_aws_demo_script.py` 驗證執行器對 `routing` 組的預覽與執行，輸出行不符（例如 `restored=no`）時整段停止。**26 項破壞驗證全部被抓到**（改壞腳本後測試會失敗：不設 EXIT 或 HUP／INT／TERM 的 trap、`restored=0` 放在 stop 之後、restore 永遠不啟動、啟動成功後不標記已恢復、暫停中三條路徑與兩個直連的預期各改一項或拿掉、不驗暫停前與恢復後的路徑與兩個池、只等一次就緒、等不到就緒也不失敗、`expect` 不失敗、入口請求加 `-k` 或拿掉 `--resolve`、直連拿掉 `-f`、停錯單元、成功行改字、組順序與歸屬、命令逾時太短、RPG 探測路徑不在分流前綴、單選時不帶找主機的步驟）。

### 串流探測（task 6.4，`scripts/aws_demo_probe_stream.py`）

**狀態：已對真實入口（`https://rac001.duckdns.org`）與真實模型執行（第 4 批，2026-11-17）：入口沒有緩衝串流；心跳經入口是否即時到達沒有驗證**（見下方「真實入口的結果」）。6.4 要求經入口量測串流有沒有被緩衝；`scripts/acceptance_rpg_turn.py` 只接受本機位址（而且需要驗收環境的小幫手與假模型），不能用，所以另寫一支最小的探測。它**不是計畫步驟**：需要登入 cookie，而 cookie 不能出現在對話、命令列或腳本裡。**實際做法（使用者在第 4 批選定）**：本機的一次性執行器（不進專案）經 SSM 在執行中的 `rac-rpg` 容器內執行 `scripts/mint_test_sessions.py --print-snippet` 印出的程式（以 base64 傳入 `docker exec -i rac-rpg python -`），簽出一位測試玩家的 cookie；cookie 只存在於執行器行程的記憶體（不印出、不寫檔），立即以環境變數 `SMOKE_SESSION_COOKIE` 交給探測腳本，探測從使用者的電腦對公開網域執行（走真實的網際網路、DNS、TLS 與 Caddy），執行器只顯示探測的輸出。**這個做法的代價**：SSM 會保存該命令的輸出（含這位測試玩家的 cookie）約 30 天，只有該 AWS 帳號可讀。另一種做法（使用者在 Session Manager 終端機自己簽 cookie、在自己的終端機設環境變數後執行探測）也可行，cookie 完全不經過 SSM 的輸出紀錄，但手動步驟較多。

- **做什麼**：取得 CSRF 權杖 → 讀劇本清單與創角資料 → 開一個測試存檔（`POST /api/sessions`）→ `POST /api/game/sessions/<id>/opening/stream`（第 0 幕，不消耗遊戲幣），記錄每個事件到達的時間。逐項輸出「通過」、「失敗」或「未驗證」；結束代碼：沒有失敗為 0、有失敗為 1、被拒絕為 2。
- **判斷「有沒有被緩衝」**：第 0 幕的第一個事件（`meta`）在模型開始產生之前就送出，`done` 在最後。入口即時轉送時，第一個事件比 `done` 早到至少 1 秒；入口緩衝整段回應時，所有事件幾乎同時到達（相差不到 0.25 秒，而且整段回應不短於 1 秒）→「失敗」；回應太短（來不及分辨）→「**未驗證**」，不當成通過。心跳（沒有其他輸出時每 15 秒一個 `ping`）：模型持續有輸出時不會出現，這一項就標為「未驗證」；有出現時檢查最大的事件間隔沒有超過 15 秒加 2 秒。
- **會呼叫模型**（開存檔的模型層審查與第 0 幕），所以必須明確加 `--allow-model-call`，沒有它就在送出任何請求之前拒絕。只對 https 的展示環境執行：主機名稱含 `production`、非 https、帶帳密、路徑、查詢字串或片段的位址一律拒絕；`--connect-address` 只接受回送位址（連到本機的入口、TLS 與 Host 仍用展示網域；cookie 不會被送到別的位址）。TLS 一律驗證憑證（不提供跳過的方法）。
- **cookie**：只從環境變數 `SMOKE_SESSION_COOKIE`（`<名稱>=<值>`，由 `scripts/mint_test_sessions.py` 在目標環境內簽出的測試玩家，與 `scripts/staging_smoke.py` 相同的慣例）讀取，沒有命令列參數；輸出只有項目名稱、數字與結果，**不含 cookie、權杖、劇情文字或任何回應內容**。測試玩家與測試存檔留在目標環境，需要時另外清除；探測不刪除任何東西。
- **真實入口的結果（第 4 批，2026-11-17；從使用者的電腦對 `https://rac001.duckdns.org`，HTTP/1.1）**：
  - **第一次執行：8 項中 3 項通過後失敗，與入口無關**。簽 cookie、取得 CSRF 權杖、劇本清單（1 份）、創角資料都通過；`POST /api/sessions` 回 **503**，延遲 30.3 秒。日誌（只讀 `docker logs`，結構化 JSON，無玩家文字）顯示開存檔的主角審查（模型層）呼叫 `nvidia/nemotron-3.5-lightning:free` **30 秒逾時**（`outcome=timeout`），審查降級（`protagonist.review_unavailable`）所以拒絕開存檔。
  - **診斷（主機內，容器內以應用程式的環境變數呼叫 `openrouter.ai`，輸出只有狀態與延遲、不含金鑰）**：到 `openrouter.ai` 的網路正常（連線 0.01 秒、TLS 0.04 秒）；金鑰有效（`/key` 回 200，`is_free_tier=False`、用量 0）；模型在清單上（免費、1M 上下文）；但同一個模型的最小請求（`max_tokens=16`）**不帶推理參數 25 秒逾時、帶 `reasoning.enabled=false` 兩次都 40 秒逾時**，串流版第一行 10.4 秒才到、整段 139.9 秒（期間為供應商的排隊心跳）——**供應商端暫時塞車**。同一時段其他模型 1 至 2 秒回應（`inclusionai/ling-3.2-flash:free` 2.2 秒、`baidu/ernie-5.5-flash-preview:free` 1.4 秒、付費的 `mistralai/mistral-nemo` 1.3 秒）。數分鐘後重測同一個模型 0.5 至 0.7 秒就恢復。結論：**免費模型的延遲不穩定**，會讓開存檔（模型層審查）與其後的回合在塞車時回 503 或降級；展示環境若要穩定，改用付費或另一個延遲穩定的模型（屬 8.3 的決定，沒有改設定）。
  - **第二次執行（模型恢復後）：7 項通過、0 項失敗、1 項未驗證，結束代碼 0**：開測試存檔回 201；串流回應標頭符合（200、`text/event-stream`、no-cache、no-transform、`X-Accel-Buffering: no`、沒有壓縮）；事件序列 `meta` 開頭、`done` 結尾、沒有 `error` 與格式錯誤；時間：回應標頭 0.78 秒、**第一個事件 0.78 秒**、第一個 token 2.05 秒、**`done` 6.11 秒**，共 18 個事件（token 16、ping 0）、文字 578 字、最大事件間隔 1.27 秒；**第一個事件比 `done` 早 5.33 秒到達 → 入口沒有緩衝串流**（Caddy 的 `flush_interval -1` 與不壓縮有效）。
  - **心跳：未驗證**。整段回應沒有出現 `ping`（`PING_SECONDS = 15.0` 是程式常數，沒有環境變數可調；模型持續有輸出時不會有心跳），所以沒有量到心跳經入口的到達時間。間接證據只有：16 個 token 事件分散在約 4 秒內、最大間隔 1.27 秒，顯示入口對每次寫出都即時轉送；心跳走同一條寫出路徑，但**沒有直接量測**。要直接驗證需要模型在 15 秒以上沒有輸出（例如供應商再塞車時第 0 幕的首字延遲超過 15 秒）或對應用程式加測試用的開關；兩者都不在本批範圍。
  - **留在展示環境的東西**：兩位測試玩家（信箱屬 `loadtest.invalid`，前綴 `loadtest-32f32d29`、`loadtest-287ce62c`，各一位）與其中一位的一個測試存檔（含第 0 幕）；探測不刪除任何東西，拆除時隨資料庫一併消失。
- **限制**：用標準函式庫的 HTTP/1.1 連線（瀏覽器走 HTTP/2；Caddy 對事件串流的轉送設定 `flush_interval -1` 與協定無關，但這裡**沒有量測 HTTP/2**）；探測的是第 0 幕，不是一般回合（兩者共用同一個串流機制與標頭）；時間包含 TLS 握手；只量了一次完整的成功執行、只從一個來源。
- **已驗證（本機）**：`tests/test_aws_demo_probe_stream.py` 以本機假伺服器走完整條流程：即時串流通過；伺服器把全部事件留到最後一次寫出（模擬被緩衝）→ 失敗；回應太短 → 未驗證；心跳準時 → 通過、事件之間沉默太久 → 失敗；`error` 結尾、沒有 `meta` 開頭、沒有 `done`、連線中途被切斷 → 事件序列失敗；非事件串流的回應 → 標頭失敗；被拒絕的 cookie 在開存檔之前就停止（沒有送出任何 POST）；伺服器要求的 Origin、CSRF 標頭與 cookie 都由探測送出；輸出不含 cookie、CSRF 權杖、劇情文字與存檔識別碼；各種不合格的位址與參數（非 https、含路徑／帳密／查詢字串／片段、含 `production`、非回送的 `--connect-address`、沒有 `--allow-model-call`、沒有或格式不對的 cookie）在建立連線之前就被拒絕；`--connect-address` 只改變 socket 連到哪裡、TLS 的 SNI 與憑證比對仍用展示網域；程式只用標準函式庫、不寫檔、不刪除。**31 項破壞驗證中 30 項被抓到**（緩衝判定與心跳判定改寬、事件序列少要求 meta 或 done、標頭少要求一項、請求少帶 Origin、CSRF 標頭或 cookie、位址守門放寬、`--connect-address` 不限回送、TLS 的 SNI 改用連線位址或不連到指定位址、跳過憑證驗證、失敗仍回 0、輸出多印 cookie 值等）；有兩項一開始沒被抓到——「事件序列容許中間出現 `error`」與「開存檔回非 201 但本文有存檔識別碼仍往下走」，是測試缺口，已補測試後都被抓到；另一項（輸出多印事件文字）我寫的改壞方式只多印了字數與標頭、並沒有真的印出劇情文字（探測根本不保存文字），不算有效的破壞驗證。**第 4 批對真實入口與真實模型的讀數見上一條「真實入口的結果」**；**仍未驗證**：心跳經入口的到達時間、HTTP/2 下的串流、一般回合（非第 0 幕）的串流。

### 告警與寄信（task 7.2、7.3）

**狀態：已於 2026-11-17（第 5 批）對真實的 AWS 與寄信服務執行。EC2 狀態告警演練成功、信件收到（收件匣）；應用程式的營運告警信第一次沒有寄出（SMTP 認證失敗），換上真正的 Brevo SMTP 金鑰後補寄成功，進入告警與已恢復兩封信都送達，但都進了垃圾郵件匣。** 這一節沒有新增計畫步驟：本批的做法都是現場的一次性操作（不進專案），結果記在這裡與 README 的驗收紀錄。

- **7.2 EC2 狀態告警（CloudWatch → SNS）**：告警 `rac-demo-staging-ec2-status-check` 早在 4.x 由 `build_plan` 的 `alarms` 組建立：`AWS/EC2` 的 `StatusCheckFailed` 最大值 ≥ 1、期間 60 秒、連續 2 期、缺資料視為正常，只有 `AlarmActions`（SNS 主題 `rac-demo-staging-alerts`，已確認的電子郵件訂閱 1 個）、沒有 `OKActions`，所以恢復成 `OK` 不會寄信。演練：`SetAlarmState` 設為 `ALARM`（05:22:25 UTC），告警歷史出現 `Action` 項目「Successfully executed action」指向該主題，約 10 秒後手動設回 `OK`（05:22:35 UTC），最後狀態 `OK`。**使用者回報信件在收件匣收到**。限度：這驗證了「告警 → SNS → 信箱」的通知路徑，不是主機真的故障時的行為；CloudWatch 自己對真實指標的判斷（連續 2 分鐘）沒有演練。
- **7.3 製造告警條件**：選「帳號鎖定激增」（門檻：目前被鎖定的帳號數 ≥ 5，`LOCKOUT_THRESHOLD`）。經 SSM 在 `rac-cs` 容器內執行一次性程式（只走 ORM：`UserRepository.add_user` 與 `lock_player`，原因 `identity_rejected`；拒絕在 `APP_ENV=production` 執行；不進專案、在 `run_script` 內以引號 heredoc 傳入），建立 5 個 `alert-drill-N@alert-drill.invalid`，`count_locked` 由 0 變 5。
- **第一次執行：條件成立但通知失敗**：`check-alerts` 輸出「帳號鎖定激增：警告（目前 5 個帳號，門檻 5 個帳號）；進入告警，通知失敗」，日誌 `alert.delivery_failed`（`reason=login_lockouts`，沒有例外內容，這是設計），指令以 1 結束。**程式在通知全部失敗時把狀態還原成搶佔之前，所以下一次檢查會再嘗試**：5 分鐘計時器在 05:25 與 05:30 UTC 自行重試，都同樣失敗（這同時證明了重試行為與計時器自行觸發）。通知失敗期間，`rac-job-check-alerts.service` 每次都以失敗結束。
- **診斷（容器內走 SMTP 各階段，輸出只有階段、例外類別、SMTP 代碼與遮蔽過地址的訊息，沒有帳密）**：`smtp-relay.brevo.com:587`、`starttls`，使用者與密碼都有設；連線成功、EHLO 250、STARTTLS 220、EHLO 250，**登入被拒：`SMTPAuthenticationError 535 5.7.8 Authentication failed`**。SSM 的 `MAIL_SMTP_PASSWORD` 長度是 9，本機被忽略的 `aws-demo.secrets` 裡同名的值也是 9 個字元、不是 Brevo SMTP 金鑰（`xsmtpsib-` 開頭、約 90 字元）的格式；`MAIL_SMTP_USER` 與 Brevo 頁面顯示的 Login 相符。結論：**建立機密時放進去的是佔位值**。`rac-cs` 是啟動時把環境檔讀進去的，所以對它 `docker exec` 讀到的是啟動當時的舊值；改完金鑰後的重新診斷改用一次性容器（先 `rac-render-env-app` 重新取出機密）。
- **修復（沒有計畫步驟的手動做法）**：使用者在 Brevo 取得真正的 SMTP 金鑰（Brevo 只在建立時顯示完整值），填進本機 `aws-demo.secrets` 的 `MAIL_SMTP_PASSWORD`（值不經對話）；我先只檢查長度（90）與格式（`xsmtpsib-`、沒有引號與空白），再以暫存檔 `{"Name", "Type": "SecureString", "Value", "Overwrite": true}`（**不帶標籤**：覆寫時指定標籤會被 AWS 拒絕；沿用既有的 `alias/aws/ssm` 金鑰）呼叫 `aws ssm put-parameter --cli-input-json file://…`，輸出遮蔽值、用後刪除，參數版本 1→2。這是因為 `put-parameter-…` 對已存在的參數會略過，而 `refresh-parameter-…` 只限由腳本組成的機密（`DATABASE_URL`）；**是否把「覆寫由 `--secrets-file` 提供的機密」補成計畫步驟待決定**。重跑 SMTP 診斷：登入 235、全部階段通過。
- **補寄與恢復**：金鑰改好後，5 分鐘計時器在 05:35:12 UTC 自行補寄，日誌 `alert.firing`（`reason=login_lockouts`、`count=5`、`severity=warning`）、輸出「進入告警，已通知」；緊接著我手動再跑一次是「狀態不變」（去重，沒有重複寄）。清除測試帳號後再跑一次 `check-alerts`：`alert.resolved`（`count=0`）、「已恢復，已通知」（05:35:59 UTC）。
- **使用者確認的結果**：兩封信（`【智能客服】告警：帳號鎖定激增（進入告警）`、`…（已恢復）`）**都在垃圾郵件匣**，沒有在收件匣。換句話說：應用程式、SMTP 與 Brevo 都把信送到了使用者的信箱，但信箱把它們判為垃圾信。**原因沒有驗證**（沒有看信件標頭，也沒有看 Brevo 的投遞紀錄）；合理的推測是寄件者位址（`MAIL_FROM`）與收件者都是免費信箱網域（gmail.com）：以免費信箱的網域當寄件者、卻由 Brevo 的伺服器寄出時，信件無法用該網域簽署 DKIM、SPF 也對不上，DMARC 不對齊，是進垃圾郵件匣的常見原因。改善要有自己的網域並在 Brevo 驗證寄件網域（SPF、DKIM、DMARC）；**展示環境不做**，列入缺口（task 7.4，見下一節「展示環境未達 production 的缺口」）：營運告警信目前可能被收件者的信箱判為垃圾信，使用者要自己把寄件者標為「非垃圾信」或定期檢查垃圾郵件匣。
- **清除**：一次性程式先確認找到的帳號「剛好是那 5 個預期的信箱」才刪（不符就中止，結束代碼 3），以 `UserRepository.delete_if_created_not_after` 刪除；結果刪 5、剩 0、鎖定數 0；告警狀態回到正常；`systemctl --failed` 為 0。沒有其他測試資料。
- **兩個池要重啟才會用新金鑰（已處理）**：`rac-cs` 與 `rac-rpg` 是啟動時把環境檔讀進去的，換掉 SSM 的值之後它們仍握著舊的佔位密碼；排程容器每次啟動都重新取機密，所以 `check-alerts` 當時已經用新金鑰，但兩個池寄的信（管理員在 `/admin` 驗證密碼那一步按「忘記密碼」會寄重設信）會失敗。使用者同意後，執行 `deploy --group restart --group verify --tag 48f5ab2 --execute`（7 步全部達成，結束代碼 0；`release.env` 仍是同一個標籤，所以 `release.previous.env` 也是它，沒有真正的前一版）。**重啟後驗證**：對**執行中的容器**做 SMTP 登入診斷（不寄信），`rac-cs` 與 `rac-rpg` 都是密碼長度 90、登入 235；兩個容器 05:45:31 UTC 啟動、`RestartCount=0`；`systemctl --failed` 為 0、五個計時器與 Caddy 仍 active；公開的 `/healthz` 與 `/readyz` 回 200。客服與 RPG 在重啟期間短暫中斷（沒有量測確切秒數）。**教訓**：換 SSM 機密之後，排程工作自動吃到新值，常駐的兩個池要重啟；換任何機密都是同一個流程。
- **證據限度**：信件有沒有送達完全是使用者的回報（我看不到信箱）；只演練了一種告警（帳號鎖定激增）與其恢復，其餘四種應用程式告警與三種平台告警（見「告警條件一覽」）沒有演練；一個收件人；Brevo 免費方案的每日寄送上限沒有查。

### 冒煙驗收與真實模型連通性（task 8.1、8.3）

**狀態**：2026-11-17（第 7 批）對真實入口 `https://rac001.duckdns.org` 執行：**冒煙驗收全部通過（含需要登入的項目）；真實 RPG 模型與客服模型各完成一次連通性驗證**；**一般模式沒有跑到**（見下方「一般與成人模式」）。8.2（Google 登入人工驗收）只能由使用者本人執行，結果另記。這一節沒有新增計畫步驟：做法是本機的一次性執行器（不進專案），結果記在這裡與 README 的驗收紀錄。

**做法**：一次性執行器經 SSM 在執行中的 `rac-rpg` 容器內執行 `scripts/mint_test_sessions.py` 印出的程式，簽出一位測試玩家（30 歲、已完成年齡確認，信箱屬 `loadtest.invalid`）；cookie 只存在於執行器行程的記憶體（不印出、不寫檔），以環境變數 `SMOKE_SESSION_COOKIE` 交給 `scripts/staging_smoke.py`，從使用者的電腦對公開網域執行（走真實的網際網路、DNS、TLS 與 Caddy）。代價同「串流探測」一節：SSM 會保存該命令的輸出（含 cookie）約 30 天，只有該 AWS 帳號可讀。所有輸出只有項目名稱、狀態碼、數字與計時，不含劇情文字、回覆內容、cookie 或權杖。

**8.1 冒煙驗收（不呼叫模型）**：10 項通過、0 項失敗、1 項略過（模型呼叫，預設略過），結束代碼 0：`/healthz` 與 `/readyz` 回 200；首頁帶五個安全標頭（含 HSTS）且沒有 CSP；沒有 CSRF 權杖的 POST 被拒絕；已移除的密碼登入與註冊端點回 404；帶測試玩家 cookie 進入、開新對話；session 與 CSRF cookie 使用 `__Host-` 前綴、Secure、無 Domain、session 為 HttpOnly；一般會員存取管理員 API 回 404；未登入存取需要登入的 API 回 401。**驗收後清除**：先核對 `@loadtest.invalid` 的帳號剛好等於預期的三個信箱（第 4 批留下的兩位加本批的一位，否則中止），再走正式的帳號抹除出口（`AccountErasureService.erase`，寫入刪除紀錄 `user_requested`），結果抹除 3、剩 0，存檔 2→0。`/readyz` 回應：狀態 200、本文只有 `{"status":"ok"}`（16 位元組）、`Cache-Control: no-store`、`Content-Type: application/json`，沒有任何內部細節。

**8.3 真實模型（同一位測試玩家，一次執行；不是容量依據）**：

| 項目 | 結果 |
|---|---|
| 模型設定（取自 `aws-demo.vars`，沒有改任何設定） | RPG：`OPENROUTER_MODEL=nvidia/nemotron-3.5-lightning:free`，沒有設 `OPENROUTER_FALLBACK_MODEL`（備援就是主要模型本身）與 `OPENROUTER_ZDR`；客服：`GEMINI_MODEL=gemini-3.5-flash-lite`；`ADULT_MODE_ENABLED` 沒有設定（成人模式關閉） |
| 遊戲幣 | 第一次讀錢包就有 200 免費幣；每日簽到成功（200→400，上限 1000）；第 0 幕不扣幣；一個回合後 370（扣 30，符合規範） |
| 開存檔 | `POST /api/sessions` 回 201，耗時 4.2 秒（開存檔的模型層審查通過，沒有再遇到 6.4 的塞車） |
| 第 0 幕（串流） | 200、`text/event-stream`；標頭與第一個事件 0.75 秒、第一個 token 1.79 秒、`done` 8.73 秒；28 個事件（`meta` 1、`token` 26、`done` 1、沒有 `error`）、813 字、最大事件間隔 1.04 秒 |
| 一個回合（串流） | 200、`text/event-stream`；標頭與第一個事件 1.15 秒、第一個 token 2.35 秒、`done` 5.89 秒；12 個事件（`meta` 1、`token` 8、`ping` 1、`state` 1、`done` 1、沒有 `error`）、351 字、最大間隔 1.20 秒；回合紀錄 1 筆，狀態 `complete`。**這個 `ping` 是回合開始、狀態呼叫之前固定送出的一個**（`app/services/rpg_turn_runner.py`），**不是 15 秒的閒置心跳**，所以 6.4 留下的「心跳未驗證」仍然成立 |
| 客服對話（Gemini） | `staging_smoke.py --allow-model-call` 全部 11 項通過，其中「送出一則訊息且回覆不是降級文案」通過（結構化輸出經 Service 驗證後得到非降級的 `reply_text`）；這也是 `GEMINI_API_KEY` 在展示環境真的可用的證據（之前只確認過長度與格式）。**生成時間沒有記錄**（冒煙驗收不輸出計時） |

**一般與成人模式（task 8.3 要求確認「一般與成人模式沒有使用免費模型的設定」）——結論：不成立，如實記下**：
- RPG 只有一個模型設定，而且是免費模型；沒有「一般模式用付費模型」這種獨立的設定，也沒有備援與零保存。
- **成人模式**：`ADULT_MODE_ENABLED` 沒有設定，所以關閉，不會用到這個免費模型。
- **一般模式**：沒有被擋。只要有宣告為一般模式的劇本、並用它開存檔，就會用這個免費模型。**這次沒有跑到**：測試存檔的 `content_tier` 是 `safe`，因為目錄裡唯一的劇本（`xianxia-qingyun`）自己宣告安全，有效模式取玩家設定、劇本宣告與年齡上限三者中最保守者；要跑一般模式必須先有宣告為一般模式的劇本（玩家創作、送審、管理員核可），不在本批範圍。
- 這項列入下方缺口表（「一般與成人模式不得使用免費模型」）。使用者在第 7 批選擇「維持免費模型」；展示環境沒有真實個資，隱私權政策也已寫明現階段免費模型不得輸入真實個資。
- **更正（2026-11-18）**：AGENTS.md 第 7、11 節於 2026-11-17 修訂後，關閉安全模式（原成人露骨）「不以取得供應商書面許可、法務意見或付費模型為前提，可使用免費模型」，只須在隱私權政策告知敏感內容可能被供應商留存或人工審閱；所以上面的「不成立」不再是缺口，缺口表第八項已撤銷（見下方）。仍成立的事實：RPG 只有一個免費模型、沒有備援（供應商塞車時回合降級或回 503）與零保存，屬上線前的模型供應商評估。

**證據限度**：一位測試玩家、各一次執行；時間包含網際網路與 TLS，不是容量依據；RPG 免費模型的延遲不穩定（6.4 曾 30 秒逾時），這次剛好順暢，不代表穩定；沒有測重新生成、編輯、記事簿抓取（要滿 10 輪）與被擋回合；一般模式與成人模式都沒有跑到。

### Google 登入人工驗收（task 8.2）

**狀態**：2026-11-17（第 7 批）由使用者本人以瀏覽器完成，我從後端日誌確認每一步的事件。**四項全部通過**；過程中發現並修正了 5.5 的管理員沒有建成（見「建立首位管理員」）。

**前置**：OAuth 用戶端沿用 `aws-demo.vars` 的 `GOOGLE_OAUTH_CLIENT_ID`（來自 `staging.vars`）。使用者在 Google 主控台把 `https://rac001.duckdns.org/auth/google/callback` 加入「已授權的重新導向 URI」（回呼網址是 `BASE_URL`＋`/auth/google/callback`，`BASE_URL` 由計畫自動填成 `https://<網域>`）；OAuth 同意畫面的發佈狀態是**測試中**，只有列為測試使用者的 Google 帳號能登入，使用者把管理員信箱與第二個 Google 信箱都加了進去。使用者回報這個用戶端除了本環境，還有一筆**舊的 Render 回呼網址**（`r-a-c-ragchat.onrender.com`），沒有 localhost 或 ngrok：它是預備性質的用戶端，不與開發共用（AGENTS.md 第 10 節），但仍與舊的 Render 環境共用同一個用戶端，那筆不用了就該移除（待使用者處理）。**預檢（不登入）**：從登入頁發起 `POST /api/auth/google/start`，取得的授權網址指向 Google 的授權端點、帶 PKCE（`S256`）、state、nonce、範圍 `openid email profile`，且 `redirect_uri` 與預期相符；對該網址做一次 GET，Google 回 302 到標準登入頁（沒有 `redirect_uri_mismatch`、`invalid_client` 或其他錯誤），代表 Google 接受這個用戶端與回呼網址。

| 項目 | 結果（後端日誌事件；沒有任何事件含信箱或個資） |
|---|---|
| ① 管理員 Google 加密碼 | `auth.google_started` → **`auth.google_linked`**（第一次以同信箱的 Google 登入，綁定到管理員，使用者 10）→ `auth.admin_google_passed`（Google 通過，等待密碼）→ `auth.login_success`（密碼通過，約 11 秒內完成），使用者確認可進入 `/admin`。**第一次嘗試失敗**：當時資料庫沒有管理員，後端把這次登入當成新玩家（`auth.google_registered`、`auth.age_confirmed.later`），`/admin` 回 404（非管理員存取後台一律 404，行為正確）；使用者同意後我走正式的抹除出口刪除該玩家，使用者重做 5.5 建立管理員，再重做這一項 |
| ② 新使用者不勾條款不建帳號（第二個 Google 帳號） | `auth.google_started` → **`auth.google_rejected.terms`**（WARNING）；沒有建帳號：下一個註冊的使用者編號沒有跳號 |
| ③ 勾選後完成年齡確認 | `auth.google_started` → `auth.google_registered`（使用者 11）→ `auth.age_confirmed.later`（約 35 秒後）；登入後預設頁是 `/game`（`DEFAULT_AFTER_LOGIN`；使用者確認是預期的）。使用者 11：角色 `user`、年齡確認完成、信箱已驗證、Google 身分 1 個 |
| ④ 回訪一鍵登入 | 第一次嘗試**沒有驗到**：使用者只是重新開啟登入頁，登入狀態還在（session 的閒置期限 30 天），被直接導向預設頁，日誌沒有 `auth.google_started`；改為先到 `/account` 按「登出」（`auth.logout`），再開 `/login` 不勾條款按 Google 登入：`auth.google_started` → **`auth.google_login`**（使用者 11），直接進入 `/game` |

**驗證後的清除（使用者決定：管理員保留、玩家抹除）**：第二個 Google 帳號的玩家（使用者 11）經正式的帳號抹除出口刪除（先核對「共 2 位、編號 11 角色是 user、遮蔽信箱相符、管理員 10 在」才動手），剩下管理員（使用者 10，已綁定 Google 身分）。

**限度與未驗證**：只驗證了**測試使用者**（發佈狀態為測試中）；Google 將同意畫面改為「正式版」之後、一般使用者登入，以及 `duckdns.org`（在公用尾碼清單上）能否當作授權網域，都沒有驗證；兩次 Google 授權都是在使用者的瀏覽器完成，我只看到後端事件；沒有驗證管理員重設密碼信（「忘記密碼」）的實際寄出。**觀察（與規範的差異，沒有修改）**：AGENTS.md 第 10 節仍寫「登入後的預設頁面目前為 /cs」，程式的 `DEFAULT_AFTER_LOGIN` 已是 `/game`（RPG 入口），規範文字已過時，是否更正由使用者決定。

### adult-only-signup 的部署與驗收（2026-11-18，映像 `87ebcd8`）

**部署**：使用者在自己的 PowerShell 分三段執行 `aws_demo.py deploy --account-id <帳號> … --tag 87ebcd8 --execute`（`--tag` 一定要明確帶新標籤：主機上原本的標籤是 `48f5ab2`；每步都要互動確認，所以不由助理代跑），每段完成後以 SSM 與 RDS 的唯讀查詢獨立複驗。這是展示環境的**第一次換版**（先前都是第一次部署的分組執行；這次沒有 `db-init`、`create-admin`）。

| 段 | 組 | 結果 |
|---|---|---|
| ① | `snapshot`、`build` | 快照 `rac-demo-staging-db-pre-87ebcd8`（手動、加密）available；映像 `rac-app:87ebcd8` 建成（SSM 的建置命令只花約 20 秒，推測是 Docker 的分層快取，沒有驗證）；`check-image` 輸出 `10001 clean` |
| ② | `migrate` | `alembic` 日誌 `Running upgrade 0018_rpg_turn_revision -> 0019_adult_declaration`（2026-11-18 10:42 +08:00）；`check-migration-head` 輸出 `1`；`seed-scripts` 建立 0、更新 0、沒有變動 1 |
| ③ | `restart`、`verify` | `release.env` ＝ `RAC_IMAGE_TAG=87ebcd8`、`release.previous.env` ＝ `RAC_IMAGE_TAG=48f5ab2`（**回復點仍在**）；`check-pools` 輸出 `rac-app:87ebcd8 rac-app:87ebcd8`；`check-pool-hardening`：兩個池 `uid=10001`、各用自己的 gunicorn 設定、只綁 `127.0.0.1:8081`／`8082`；`NRestarts=0`、`restartcount=0`、`systemctl --failed` 為 0、最近 15 分鐘日誌的 ERROR／CRITICAL 為 0 筆；公開 `/healthz`、`/readyz`、`/login` 皆 200 |

- `switch-release` 在 SSM 出現兩筆（相隔 23 秒）：第二筆的原因沒有追查。`_switch_release_lines` 對同一個標籤**不覆蓋** `release.previous.env`，主機上的檔案也證實前一版仍是 `48f5ab2`。
- **回復**只換回前一版標籤（`rollback`），不降級資料庫（0019 是 expand-only，舊版不讀新欄位）。**沒有演練**；遷移到換版之間（約 2 分鐘）舊版池在新結構上的實際運作也沒有另外探測，只依設計推定。

**Google 登入人工驗收**（使用者本人，`https://rac001.duckdns.org/login`）：五個情境全部通過：①只勾條款、不勾「我確認已滿 18 歲」→ 不建立帳號並顯示提示；②兩項都勾 → 進年齡確認頁；③填 15 歲生日 → 帳號被抹除並回登入頁；④同一信箱重新註冊、填 25 歲 → 通過、預設安全模式、贈 200 幣；⑤回訪者不勾任何項目、一鍵登入。日誌核對只計**事件名稱**的次數（不讀內容）：`auth.google_rejected.adult` 1、`auth.age_rejected.underage` 1、`account.underage_erased` 1、`auth.age_confirmed.later` 1、`wallet.signup_granted` 1、`auth.google_login` 1，`auth.google_rejected.terms` 0；全在客服池。**與任務原文的差異**：`age_not_eligible` 是 API 錯誤代碼、不是日誌事件（日誌是 `auth.age_rejected.underage` 加 `account.underage_erased`）；`auth.age_confirmed` 實際叫 `auth.age_confirmed.later`。

**驗收帳號的清除**：專案沒有「立即抹除單一已確認帳號」的 flask 指令（既有的清除都有條件：寬限期滿、閒置、逾時未確認、未滿 18 歲），所以用一支**不進版本庫的一次性腳本**在主機的一次性容器執行，只呼叫既有的 `UserRepository.request_deletion` 與 `AccountErasureService.erase_pending_due`（兩段都排除管理員、都寫刪除紀錄），並要求「非管理員帳號恰好 1 個、編號相符、`active`、已聲明並確認、當天建立」才動手；先在本機 SQLite 以假帳號實測（錯誤編號、時間不符、兩個玩家都被拒絕），再於主機預覽與執行，事後只剩管理員。**沒有獨立查證**刪除紀錄（`deletion_tombstones`）與 `account.deleted_by_request` 事件。是否補成正式的維運指令（須經 OpenSpec、測試與稽核）尚未決定，排在後面。

**對展示的影響**（2026-11-24、11-25 的展示前要知道）：①展示環境現在**沒有玩家帳號**，要自己用 Google 登入新註冊；②新玩家一律預設**安全模式**（先前成年玩家預設一般模式），安全模式看不到宣告為一般或成人模式的劇本；③目前**沒有任何前端畫面能切換內容模式**（`PUT /api/account/content-mode` 沒有前端呼叫，帳號頁沒有切換），新玩家無法從介面離開安全模式；④展示環境沒有開成人模式（部署計畫與 `aws-demo.vars` 都沒設 `ADULT_MODE_ENABLED`，預設關閉）。②至④要靠 `safe-mode-toggle` 才能改善，是否為展示提前做最小的切換由使用者決定。

**未驗證**：登入頁在 360 px 手機寬度的版面；客服提示詞改過之後用真實 Gemini 問「怎麼註冊」；容器驗收只用 SQLite、沒有連 SQL Server（SQL Server 由本節的真實部署涵蓋）；情境②沒有專屬日誌事件，只靠使用者目視；刪除紀錄的獨立查證如上。費用的證實數字見 README「驗收紀錄（aws-demo-environment）」的每日費用觀察。

### 展示環境未達 production 的缺口（task 7.4）

**狀態**：2026-11-17 整理（第 6 批；第 7 批依 8.3 的結果加第八項「一般與成人模式不得使用免費模型」，**2026-11-18 因 AGENTS.md 修訂而撤銷，現為七項**）。這一節只整理文件，沒有對 AWS 執行任何指令；「展示環境現況」取自計畫、design D11 與 task 4.x 至 7.x 的實際紀錄，本批沒有重新查詢。

展示環境不是 production（見本節開頭的限制）。下表列出它與 AGENTS.md 第 9、11 節要求之間的缺口：每項寫明展示環境現況、不做的原因、風險與暫行做法、何時重新評估。不做的共同原因是展示用途（沒有真實會員，展示期到 2026-12-31）、費用與時程（design D11），**不是技術上做不到**。已列在「未滿足的規範項目」的入口層 IP 限流與內容安全政策，在展示環境同樣沒有（Caddy 不限流、應用程式不送 CSP），不在此重複。

| 缺口（規範來源） | 展示環境現況 | 原因 | 風險與暫行做法 | 重新評估 |
|---|---|---|---|---|
| **5xx 比率告警**（第 11 節；平台告警：5xx 占全部請求超過 5%、窗口內至少 20 個請求、300 秒） | 沒有。入口是主機上的 Caddy，沒有現成的請求計數指標；`check-alerts` 只評估資料庫量得到的五項，不含 HTTP 5xx | 展示用途、沒有真實會員；要做需要額外元件（日誌型指標或自己的探測），不值得（design D11） | 應用程式或資料庫壞到回 5xx 時沒有人被通知。暫行：每批開工時用唯讀的 `deploy --group verify` 與公開的 `/healthz`、`/readyz` 檢查，需要時看 `docker logs`（結構化 JSON，含狀態碼） | 有真實會員之前；或 `rpg-ops-loadtest` 要在無人看管下長時間執行之前 |
| **外部存活與就緒探測**（第 11 節；`/healthz`、`/readyz` 各連續失敗 3 次，探測要來自服務之外） | 沒有。只有 EC2 `StatusCheckFailed` 告警：它看的是主機本身，看不到 Caddy 或兩個池停了，也看不到資料庫連不上；兩個池的單元有 `Restart=always`，行程死掉會自動重啟，但重啟迴圈與資料庫不通不會通知 | 同上 | 主機健康、入口或池卻停了的時候沒有人知道 | 把展示環境給使用者以外的人看之前；有真實會員之前 |
| **寄信失敗告警**（第 11 節；`mail.send_failed` 300 秒內 3 次以上） | 沒有。**第 5 批實際發生過**：SMTP 金鑰是佔位值，`check-alerts` 的通知失敗後每 5 分鐘重試、每次都失敗，**沒有任何人被通知**，是人工執行 `check-alerts` 才發現的。平台告警走 SNS，與 SMTP 互相獨立，這正是它存在的原因 | 同上 | 營運告警信寄不出去時沒有人知道。暫行：換任何機密之後手動跑一次 `check-alerts` 並查 `systemctl --failed`，兩個池要重啟（流程見「告警與寄信」） | 同上 |
| **排程失敗與「告警檢查停擺」告警**（第 11 節；四個排程工作任一個非零結束；`check-alerts` 超過 15 分鐘沒有 `alert.check_completed`） | 沒有。失敗只留在 `systemctl --failed` 與 journald；五個計時器只有 `check-alerts`（每 5 分鐘）看到自行觸發，其餘四個每小時或每日的還沒等到觸發時間 | 同上 | 保存期限清除、帳號刪除、閒置與逾時帳號清除失敗時沒有人知道。展示環境沒有真實會員資料，影響很小；到 production 前這些是隱私義務的一部分，風險大。暫行：開工時以 SSM 唯讀查 `systemctl --failed` | 同上；其餘四個計時器的實際觸發結果在 8.4 的驗收紀錄補上 |
| **備份保留 30 天、跨帳號或跨區域存放、每季還原演練**（第 11 節） | RDS 自動備份只保留 **1 天**（Free plan 不接受 7 天，AWS 回 `FreeTierRestrictionError`，2026-11-16；規格只要求大於 0）；與資料庫同帳號同區域（雪梨）、單一可用區；加密已開；每次部署前另有手動快照 `<資料庫>-pre-<提交短碼>`（不隨 RDS 消失；拆除時預設刪除，見 design D14）。**還原演練沒有做** | Free plan 的限制加上展示用途；跨帳號或跨區域複製是額外的費用與流程 | 時間點還原只能回到最近 1 天；資料毀損超過一天才發現就回不去（展示環境的資料可重建：測試帳號與種子）。手動快照是唯一較長的保留，但留著會持續計費。暫行：遷移之前一定先快照 | 資料變得不可重建之前（有真實會員，或要保留 `rpg-ops-loadtest` 的量測資料）；改用付費方案或搬到 production 時：保留提高到 30 天、加跨帳號或跨區域複製，並做第一次還原演練 |
| **應用程式日誌保存 30 天**（第 11 節） | 沒有集中式日誌。每個容器只有 Docker 的循環日誌（`json-file`，單檔 10 MB、最多 3 個，每個容器上限約 30 MB，**依大小而不是依時間**，流量大時保存更短）；單元的輸出依 systemd 預設也進 journald（單元沒有另外設定輸出；排程單元用 `journalctl -u` 讀到過，常駐單元沒有另外驗證），但保存時間與是否跨重開機保存**沒有設定也沒有查證**；排程容器是 `--rm`，容器日誌隨容器消失，只剩 journald；主機終止時日誌全部消失。格式（結構化 JSON、不含個資）符合規範 | 沒有日誌代理或集中式服務；費用與時程 | 事後調查（幾天前的 5xx、告警失敗、模型逾時）可能已經沒有日誌。暫行：事件發生時立即看 `docker logs` 與 `journalctl -o cat`（本 change 的雲端批次都是這樣做） | 有真實會員之前；或需要保留量測期間的日誌時（`rpg-ops-loadtest`） |
| **營運告警信的投遞品質**（上線前關卡「網域取得，Google 登入與信件的 SPF／DKIM／DMARC 設定」） | 進入告警與已恢復兩封信**都進了使用者的垃圾郵件匣**；原因沒有驗證（沒看標頭與 Brevo 的投遞紀錄），推測是寄件者與收件者都是免費信箱網域，DKIM 與 SPF 無法對齊。只有**一個收件人**；Brevo 免費方案的每日寄送上限**沒有查**；管理員重設密碼信**沒有真的寄過**（只驗證了兩個池的 SMTP 登入）。EC2 告警（SNS）的信在收件匣收到 | 沒有自己的網域可以在 Brevo 驗證寄件網域；展示環境不做 | 告警信可能被信箱判為垃圾信而漏看。暫行：使用者自己把寄件者標為「非垃圾信」並定期看垃圾郵件匣 | 取得自己的網域時（與上線前關卡同一項）；增加收件人或寄信量之前先查寄送上限 |
| ~~一般與成人模式不得使用免費模型~~ **（2026-11-18 撤銷，見表後說明）**（原依據：第 11 節「RPG 模型供應商與個資」；第 7 批 8.3 新增） | RPG 只有一個模型設定，是免費模型 `nvidia/nemotron-3.5-lightning:free`，沒有備援（`OPENROUTER_FALLBACK_MODEL` 未設，備援就是主要模型）、沒有零保存（`OPENROUTER_ZDR` 未設）。成人模式沒有開（`ADULT_MODE_ENABLED` 未設），不會用到它；**一般模式沒有被擋**：有宣告為一般模式的劇本時，用它開的存檔就用這個免費模型。目前目錄只有一份劇本、宣告安全，所以一般模式在展示環境沒有跑過。供應商塞車時開存檔回 503（6.4 觀察） | 使用者選擇維持免費模型（第 7 批）；展示環境沒有真實個資，隱私權政策已寫明現階段免費模型不得輸入真實個資；付費模型與零保存的費用與設定屬上線前評估 | 若有人建立宣告為一般模式的劇本再用它開存檔，輸入輸出會送到免費模型的供應商，可能被留存；供應商塞車或故障時沒有備援，回合降級或回 503。暫行：只用測試帳號、不輸入真實個資 | 對外開放真實會員之前（上線前關卡「模型供應商方案與資料使用條款的重新評估」）；改用付費模型時同時設定備援模型與 `OPENROUTER_ZDR` |

**不是缺口、但仍未驗證**（只是還沒量到，不代表達不到要求；逐項見 README「驗收紀錄（aws-demo-environment）」的未驗證段）：心跳經入口的到達時間、HTTP/2 下的串流、一般回合（非第 0 幕）的串流、其餘四個排程計時器的實際觸發、Caddy 憑證續期、`rollback` 與 `teardown` 的真實執行。

**撤銷第八項（2026-11-18）**：AGENTS.md 第 7、11 節於 2026-11-17 修訂：全站只限滿 18 歲、內容模式為「安全模式開／關」，**關閉安全模式不以付費模型為前提，可使用免費模型**（輸入輸出可能被供應商留存或人工審閱，隱私權政策須告知）。因此「一般與成人模式不得使用免費模型」不再是規範要求，這一列保留原文與刪除線以備查，不計入缺口。該列最後一欄的事實仍有參考價值（沒有備援與零保存），併入上線前的模型供應商評估。

**已處理、不再是缺口**：兩個池曾握著舊的佔位 SMTP 密碼，已於 2026-11-17 重啟並驗證（見「告警與寄信」）。

**怎麼用這張表**：升級成 production 或接到真實會員之前逐項處理；某一項達成後在原處標明「已達成」、日期與依據，不得悄悄刪掉（同「未滿足的規範項目」的規則）。

---

**何時更新本文**：環境與必要機密、容器與 gunicorn 設定、migration 的部署步驟、日誌白名單或事件、維運指令、保存期限的實作狀態、測試分層與驗收數字改變時。`tests/test_design_docs.py` 會檢查四種環境名稱與每個 flask 指令都出現在本文。
