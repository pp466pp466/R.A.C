# Proposal

## Why

AGENTS.md 第 9 節與第 11 節對上線有一串要求，C-4 系列做完後，程式庫裡**還缺**的有：

- **傳輸安全**：規範要求入口層啟用 HTTPS 並設定 HSTS，但應用程式沒有輸出 HSTS 或任何安全標頭（目前只有 ProxyFix）。Cloud Run 不會替我們加。
- **部署定義**：專案有映像與 gunicorn 設定，但沒有任何雲端部署的定義：機密怎麼對應到環境變數、migration 作為獨立步驟怎麼跑、四個排程指令（`check-alerts` 每 5 分鐘、另三個每日）何時執行，全都只存在文件的一句話裡。
- **平台負責的告警**：C-4d 明確把 5xx 比率、服務存活、寄信失敗留給平台，條件只寫在營運文件。
- **備份還原後的重新刪除**：規範要求「還原時依 `deletion_tombstones` 重新執行刪除」，但**沒有這個指令**，只有一張表。這是個資法刪除權的洞：使用者刪了帳號，一次備份還原就讓他復活。
- **負載測試**：gunicorn 的 worker 與執行緒數是 D-13 的估算初值，容量數字都是假設，從未量過。
- **營運手冊與上線前關卡**：部署、回復、金鑰輪替、還原演練的步驟，以及 6 項上線前關卡，散落在 report.md 與各文件。

依使用者決定，C-5 拆成兩個 change：**本 change（C-5a）只做程式庫內的部分**，不碰雲端帳號、不產生費用；C-5b 才在使用者的 Google Cloud 專案建立預備環境、跑壓測與還原演練、完成正式上線。

## What Changes

- **安全標頭**：staging 與 production 的每個回應（含錯誤與靜態資源）加上 `Strict-Transport-Security`（一年，不含 `includeSubDomains` 與 `preload`）、`X-Content-Type-Options: nosniff`、`Referrer-Policy: strict-origin-when-cross-origin`、`X-Frame-Options: DENY`、`Permissions-Policy`（停用相機、麥克風、定位等）。development 與 testing 不送 HSTS（本機是 http，瀏覽器會把 HSTS 記在 localhost）。**不做內容安全政策（CSP）**：前端使用 Vue 全域建置，模板在瀏覽器編譯，需要 `unsafe-eval`，這樣的 CSP 幾乎沒有保護力；記為已知限制。
- **部署計畫腳本** `scripts/gcp_launch.py`（Python 加 `gcloud`，符合第 8 節）：**預設只印出將執行的指令（dry run）**，加 `--execute` 才逐步執行並在每一步前要求確認。子命令涵蓋：機密（每個機密變數對應 Secret Manager 與 Cloud Run 的 `--set-secrets`，一般設定為 `--set-env-vars`）、建置映像、migration（以 Cloud Run Job 執行 `alembic upgrade head`，先於新版程式部署）、部署服務、排程（四個指令各建一個 Cloud Run Job 與 Cloud Scheduler）。計畫本身是純函式（`scripts/launch_plan.py`），不含任何機密值，測試不需要 `gcloud`。
- **平台告警定義**：三項平台告警（5xx 比率超過 5%、`/healthz` 與 `/readyz` 外部探測連續失敗、日誌事件 `mail.send_failed`）加上**排程工作失敗**（指令以非零代碼結束），以 Cloud Monitoring 的日誌型指標、uptime check 與 alert policy 表達，通知管道為電子郵件。同樣由腳本產生、預設 dry run。
- **新增 `flask replay-deletions [--dry-run] [--ids-file 檔案]`**：備份還原後、對外開放流量**之前**執行，依資料庫中的 `deletion_tombstones`（以及由日誌取出的 `使用者編號,刪除時間` 檔，涵蓋備份之後才發生的刪除）重新抹除；走既有的 `AccountErasureService` 單一出口。**只抹除「該帳號在刪除時間之前就存在」的使用者**，避免還原後被重新發出的編號誤刪別人；絕不抹除管理員。
- **還原演練腳本** `scripts/restore_drill.py`：把備份還原到暫存資料庫、驗證結構版本、執行 `replay-deletions`（先 dry run 再實際），檢查已刪除的帳號沒有復活，輸出只有筆數。
- **負載測試** `loadtest/`：Locust 情境（註冊登入 → 開對話 → 每 30 至 45 秒送訊息的混合行為）、**假模型**的應用程式入口（可設定延遲與錯誤率；只在 development 與 testing 載入，production 與 staging 拒絕啟動）、通過標準的判定腳本（p95 小於 8 秒、錯誤率小於 1%、無資料庫鎖定錯誤；CPU 需在平台量測）。本機結果**只用來驗證測試本身可用**，不當作容量依據；gunicorn 的調整留給 C-5b 在預備環境依實測決定。
- **營運手冊與上線前關卡**：營運文件新增部署步驟、回復、機密輪替（90 天）、還原演練與事故處理；新增「上線前關卡」清單（信箱驗證、模型供應商方案（Gemini 與 OpenRouter）、網域與 SPF/DKIM/DMARC、法務覆核、資料庫成本、負載測試），每項標明負責人與目前狀態。
- **依使用者決定，不在本 change**：在雲端實際建立資源與壓測（C-5b）；**入口層的 IP 限流**（使用者決定先不做；AGENTS.md 第 9 節「第一道限流使用入口層」因此**尚未滿足**，列為上線前關卡的未結項目，不修改 AGENTS.md）；CSP；RPG 專用的獨立 worker 池（RPG 尚未實作）；gunicorn 參數調整。
- 新增 `service-observability` 的兩個事件：`account.deletion_replayed`（只有內部編號）與 `alert.check_completed`（`check-alerts` 完整跑完一次的心跳，只有事件名稱；平台用它偵測告警檢查停擺）。這是對 C-4d 程式的唯一回頭修改。

## Capabilities

### New Capabilities
- `security-headers`: staging 與 production 的安全標頭（HSTS、nosniff、Referrer-Policy、框架禁止、Permissions-Policy）、development 與 testing 不送 HSTS、錯誤回應與靜態資源同樣套用、不做 CSP 的明文限制。
- `deployment-plan`: 以 Python 腳本表達的雲端部署計畫：預設 dry run、機密與一般設定的對應與完整性、migration 作為先於部署的獨立步驟、四個排程指令的頻率與執行方式、計畫不含機密值。
- `platform-alerts`: 平台負責的告警定義：5xx 比率、服務存活與就緒探測、寄信失敗事件、排程工作失敗；條件、窗口與通知管道。
- `deletion-replay`: 備份還原後依刪除紀錄重新抹除（資料庫紀錄加日誌取出的檔案）、編號被重新發出時不誤刪、不抹除管理員、可預覽與重複執行、還原演練。
- `load-testing`: 負載測試的情境、假模型入口的環境限制、通過標準的判定、測試只在測試或預備環境執行。
- `launch-gates`: 上線前關卡清單與營運手冊的必要內容（每項關卡的負責人與狀態、未結的規範缺口必須明列）。

### Modified Capabilities
- `service-observability`: 新增 `account.deletion_replayed` 事件只含內部編號、`alert.check_completed` 心跳事件只含事件名稱且只在完整成功時記錄的要求。

## Impact

- **新增**：`app/security/security_headers.py`；`app/services/deletion_replay_service.py`、`flask replay-deletions`；`UserRepository` 的條件式刪除（創建時間比對）；`scripts/gcp_launch.py`、`scripts/launch_plan.py`、`scripts/restore_drill.py`；`loadtest/`（locustfile、假模型入口、判定腳本）；`app/observability/lifecycle_events.py` 增加 `account.deletion_replayed`、`app/observability/alert_events.py` 增加 `alert.check_completed`（並由 `AlertService`／`check-alerts` 在完整成功時記錄）。
- **修改**：`app/__init__.py`（註冊標頭）、`app/cli.py`、`app/services/registry.py`、`requirements-dev.txt`（Locust，僅開發用，不進映像）、`.dockerignore` 確認不含 `loadtest/`。
- **文件**：營運與部署（部署步驟、回復、輪替、還原、事故處理、上線前關卡、平台告警定義）、系統架構、路線圖、README；`tests/test_design_docs.py` 會檢查指令與事件。
- **風險**：①腳本只在 dry run 與測試中驗證過，**沒有對真實的 `gcloud` 與雲端專案執行**，真正的驗證在 C-5b；②IP 限流缺口讓入口沒有第一道防線，上線前必須重新決定；③HSTS 一旦被瀏覽器記住就難以撤回，所以只設一年且不含子網域與預載入；④`replay-deletions` 是刪除動作，必須有 dry run、條件式刪除與演練；⑤本機負載測試的數字不是容量依據。
