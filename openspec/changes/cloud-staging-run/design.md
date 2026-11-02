# Design

## Context

> 本 change 是 `staging-deployment`（C-5b，程式、文件與本機預演）之後的**雲端執行**。它假設：`GEMINI_BASE_URL`、假模型伺服器、階梯式加壓、冒煙驗收腳本、開發用通道與本機預演已完成；使用者已有**可計費的 Google Cloud 專案**。以下決策原本寫在同一份設計裡，D11 至 D13（冒煙腳本、開發用通道、Google 登入）屬於 `staging-deployment`。

C-5a（`launch-readiness`，已封存）交付了：`scripts/launch_plan.py`（純函式的步驟資料）、`scripts/gcp_launch.py`（預設預覽、`--execute` 逐步確認）、平台告警定義、`restore_drill.py`（本機 SQL Server 的 `RESTORE`）、`loadtest/`（Locust、本機假供應商、位址守門、判定腳本）。**這些只在預覽輸出與假 runner 下驗證，從未對真實的 Google Cloud 執行。** 現況（已查證）：

- `.env` 只有本機開發用的變數（`APP_ENV=development`、`BASE_URL=http://localhost…`、兩把模型金鑰、Google OAuth 的開發用戶端、`TEST_SQLSERVER_URL`），**沒有寄信設定與雲端專案**。
- Google 登入的回呼網址取自 `BASE_URL`（`app/__init__.py`），不取自請求；CSRF 檢查比對的是**請求自己的來源**（`request.host_url`），且只有 staging 與 production 掛 ProxyFix。
- `GeminiProvider` 已有 `base_url` 建構參數，但 `create_app` 沒有把設定傳進去；金鑰在 `x-goog-api-key` 標頭，回應解析 `candidates[0].content.parts[].text` 與 `usageMetadata`。
- `create-admin` 只能互動輸入或 `--password-stdin`；Cloud Run 工作兩者都不行。
- 動機與範圍見 proposal.md。

## Goals / Non-Goals

**Goals:**
- 預備環境真的建起來、真的通過冒煙驗收，並且**C-5a 腳本與雲端的差異全部回寫並有測試**。
- 容量數字來自雲端而非本機；gunicorn 與 Cloud Run 的尺寸有實測依據與記錄。
- 每個產生費用或不可逆的步驟，使用者都在場並明確確認；不在場就停下。

**Non-Goals:**
- production 資源與對外開放（C-5c）；自有網域、SPF／DKIM／DMARC；IP 限流；法務與供應商方案。
- 為了通過驗證而放寬安全規則（例如 `TrustServerCertificate=yes`）。

## Decisions

### D1：執行順序與「發現就回寫」

依序：前提確認 → 準備（API、服務帳號、機密）→ **資料庫與憑證探測** → 建置 → migration → 部署 → 冒煙 → 排程 → 平台告警 → 告警演練 → 壓測環境 → 容量量測與調整 → 還原演練。每一組：預覽 → 逐步確認執行 → 以唯讀指令驗證 → 記錄。**只要 `gcloud` 的行為與 `launch_plan.py` 不同，先修計畫與測試再繼續**（這個 change 的主要產出之一）。雲端執行需要使用者在場；不在場時停在上一個已完成的步驟，並把狀態記在 README 的驗收紀錄。

### D2：前提與費用是第一道關卡，沒有預設值

進入任何雲端步驟前先確認（記在 README，不含機密）：Google Cloud 專案識別碼與已啟用計費、區域、資料庫的版本與規格（含使用者看過的費用估算後同意）、寄信服務（SMTP 主機、連接埠、安全性、寄件者、帳密）、預備環境專用的 Google OAuth 用戶端（開發與正式使用不同用戶端，這一步只能在 Google Cloud 主控台做）。`launch_plan.py` 的資料庫參數**沒有預設值**，建立執行個體還需要 `--accept-cost`（`--execute` 的逐步確認之外的第二道）。成本不在程式裡估算（價格會變），由使用者在主控台的計價頁確認。

### D3：伺服器憑證驗證——先探測，不退讓

Cloud SQL 的伺服器憑證由「每個執行個體自己的 CA」簽發，私有 IP 連線沒有可比對的主機名稱，所以單純 `TrustServerCertificate=no` 會失敗。候選做法依序嘗試：
1. ODBC Driver 18 的 `ServerCertificate=<憑證檔路徑>`（把執行個體的伺服器 CA 從 `gcloud sql instances describe` 取出，存入 Secret Manager，以**檔案掛載**進容器），必要時加 `HostNameInCertificate`；
2. 把該 CA 併入系統憑證組合檔，並以 `SSL_CERT_FILE` 指向——**但這會影響整個行程的 TLS 驗證**（Gemini、SMTP、OAuth 也讀它），所以組合檔必須包含系統 CA；在容器啟動時組合，工作量與風險較大，列為第二選擇。

探測方式：建立執行個體後，立刻以一個 Cloud Run 工作（含私有網路）做**加密並驗證憑證的連線測試**（`SELECT 1`），在 migration 與部署之前。成功的連線字串型態寫回計畫與文件。**兩種都失敗就停止並回報使用者**，不得改用 `TrustServerCertificate=yes`（AGENTS.md 第 9 節：正式與預備環境嚴禁）。計畫需要支援「機密以檔案掛載」（`--set-secrets=/path=secret:latest`），目前只有環境變數的形式。

### D4：私有網路

使用專案的預設 VPC（除非使用者指定），建立 Private Services Access（保留位址範圍加 VPC peering）給 Cloud SQL；Cloud Run 服務與工作以 Direct VPC egress（`--network`／`--subnet`，`private-ranges-only`）連到私有 IP。其餘對外呼叫（Gemini、SMTP）走一般網際網路。**C-5a 的 `--network`／`--subnet` 旗標在此第一次實測。**

### D5：資料庫登入的密碼不經命令列

`gcloud sql users create` 的 `--password` 會留在行程列表。改用 `--prompt-for-password` 並讓指令**直接繼承終端機**（新增 `Step.interactive`：執行器不擷取其輸入輸出，由 gcloud 自己提示使用者，值從不經過我們的行程）。`DATABASE_URL` 機密則由執行器以擷取到的私有 IP、登入名稱、資料庫名稱與使用者再輸入一次的密碼**組成**（`prompt:` 之外新增一個「由部分組成」的來源），經標準輸入寫入 Secret Manager；密碼以 URL 編碼，不出現在參數、輸出與日誌。

### D6：壓測走真實程式路徑，只把「模型」換成假的

不把測試程式放進主映像（C-5a 已決定）、也不在預備環境用真實 Gemini（免費層每分鐘約 5 次，100 位虛擬使用者無從談起）。做法：
- 新增設定 `GEMINI_BASE_URL`（`app/config.py`，`create_app` 傳給 `GeminiProvider`）。**production 拒絕**；只允許 https（development 與 testing 另允許回送位址的 http），不得含帳密、查詢與片段。
- `loadtest/fake_llm/`：一個小型 Flask 應用（Gemini 相容的 `generateContent`、`x-goog-api-key` 必須等於部署時設定的測試金鑰、延遲與錯誤率環境變數、日誌只有彙總）、自己的 `Dockerfile` 與 `requirements.txt`；以 `gcloud builds submit loadtest/fake_llm` 建置（建置內容根目錄是那個子目錄，與主映像互不影響）。公開的 `*.run.app`（`--allow-unauthenticated`），靠測試金鑰與最大執行個體數限制濫用。
- 壓測 profile（`Target.profile = "loadtest"`）：同一個主映像、**自己的服務、資料庫、登入、機密與假金鑰**，`APP_ENV=staging`、`GEMINI_BASE_URL` 指向假伺服器；與預備環境互不共用（同一個 Cloud SQL 執行個體，不同資料庫，費用不增加）。
- 為什麼不用更簡單的做法：在預備環境直接換入 `LoadTestProvider` 需要把測試程式放進映像或加一個「載入替身」的環境開關，兩者都擴大正式映像的攻擊面；`GEMINI_BASE_URL` 是一個單純、可驗證、production 拒絕的設定。

### D7：容量量測與調整是有紀律的迴圈

基準：`WEB_CONCURRENCY=2`、`GUNICORN_THREADS=8`、1 vCPU、512Mi、並行數 16（C-5a 的預設）。每一輪：階梯式加壓（`loadtest/shapes.py`）→ 以 `evaluate.py` 判定（CPU 由 `loadtest/cloud_cpu.py` 向 Cloud Monitoring 取壓測期間第 95 百分位的 CPU 使用率最大值；取不到就是未驗證）→ 記錄該輪設定與結果 → 依瓶頸調整**一個**變數（先 worker／執行緒，再 CPU／記憶體，最後並行數與執行個體數）。最終值寫回 `gunicorn.conf.py` 的預設與計畫的尺寸預設，並附對應那一輪的結果；**達不到標準就寫明最佳結果與瓶頸，關卡維持進行中**（不為了好看而放寬標準）。

### D8：告警演練只做可逆的

| 類別 | 觸發方式 | 復原 |
|---|---|---|
| 排程工作失敗 | 以錯誤的參數執行一次 `check-alerts` 工作 | 不需要（單次失敗） |
| 告警檢查停擺 | 暫停 `check-alerts` 的排程超過 15 分鐘 | 恢復排程 |
| 服務就緒 | 部署一個資料庫連線指向無效機密版本的修訂並導入全部流量 | 把流量切回前一個修訂 |
| 服務存活 | 暫時把預備服務的 ingress 改為 `internal`，外部探測連不到 | 改回 `all` |
| 寄信失敗 | 暫時換成無效的 SMTP 密碼版本並觸發需要寄信的流程 | 還原機密版本 |
| 5xx 比率 | 在**壓測服務**暫時把資料庫連線指向無效的機密版本，再以 Locust 小量持續送請求（窗口內至少 20 個，結果多為 5xx）；假模型回 500 不算（那是模型錯誤，應用程式會降級回覆 200） | 還原機密版本 |

演練只在預備環境或壓測環境；5xx 以壓測服務演練以免汙染預備環境的指標。**沒收到通知就標為未驗證**，不以預期代替。

### D9：還原演練複製整個執行個體

`gcloud sql instances clone SOURCE <名稱含 _restore_drill 的目標> --point-in-time=…`（需要時間點還原已開啟）。在複製上用一個 Cloud Run 工作（同一個映像、含私有網路）依序執行 `replay-deletions` 的預覽、實際、再一次，刪除紀錄檔案以**機密檔案掛載**提供（只有編號與時間）。結束後（成功或失敗）刪除複製、工作與機密；量測複製完成的時間作為還原耗時的基準。來源只做唯讀的 `describe`。

### D10：預備環境的暴露面

`*.run.app` 預設公開。緩解：每日用量上限（`USAGE_DAILY_TOKEN_LIMIT`、`USAGE_DAILY_CALL_LIMIT`）設為小值、`--max-instances` 小、帳號只用 `.invalid`、不放真實個資、`EMAIL_VERIFICATION_REQUIRED=false`（預備環境允許）；寄信設定仍必須齊全（因為 `ALERT_EMAIL_TO` 在 staging 必填）。使用者決定營運告警信**只寄給自己**，寄信用使用者的 **Brevo** SMTP 中繼：`MAIL_SMTP_HOST=smtp-relay.brevo.com`、`MAIL_SMTP_PORT=587`、`MAIL_SMTP_SECURITY=starttls`；`MAIL_SMTP_USER` 是 Brevo 後台「SMTP 與 API」頁顯示的 SMTP 登入名稱；`MAIL_SMTP_PASSWORD` 是該頁產生的 **SMTP 金鑰**（不是帳號密碼，走 Secret Manager）；`MAIL_FROM` 必須是 Brevo 已驗證的寄件者。專案的寄信器只做標準 SMTP（STARTTLS 加帳密），不需要改程式。Brevo 免費方案每天有寄信上限（約 300 封），遠超過預備環境的告警量。**送達是主要風險**：若 `MAIL_FROM` 是 `gmail.com` 這類免費信箱，Gmail 會看到「寄件者宣稱是 gmail.com、卻不是 Google 的伺服器寄的」，可能進垃圾郵件甚至被拒；Brevo 對免費信箱當寄件者也可能有自己的限制。所以實作時先寄一封測試信（以 `flask check-alerts` 觸發一次或單獨寄一封）確認收得到，寄不到就改用 Gmail SMTP（寄件者等於登入帳號、應用程式密碼）或日後自有網域加 DKIM 的 Brevo——都只換一般設定與密碼。自有網域取得後（C-5c），Brevo 加上網域驗證（SPF／DKIM）是更好的長期解法，且可同時用於 production 的驗證信。收件人地址不是機密但屬於個人資訊，只放 `--vars-file` 與 `--notify-email`，不進程式庫。信箱驗證的雲端流程因此**未驗證**（預備環境關閉驗證，也沒有可收信的測試信箱），記在驗收紀錄。




## Risks / Trade-offs

- **費用** → 每個計費資源建立前顯示並確認；執行個體建立需要 `--accept-cost`；還原演練與壓測結束後刪除暫存資源；驗收紀錄列出仍在計費的資源。
- **憑證驗證可能行不通** → D3 的探測在任何部署之前；失敗就停止，不退讓。這可能讓 C-5b 在第一個雲端步驟就卡住，但這是正確的結果。
- **`gcloud` 與 Cloud Monitoring 的 JSON 細節在 C-5a 無法驗證** → D1 的回寫迴圈；預期會有不少修正，已為此預留任務。
- **公開的預備環境與假模型伺服器可能被濫用** → D10 與假伺服器的測試金鑰、最大執行個體數；金鑰與使用者資料都是假的或測試用。
- **`GEMINI_BASE_URL` 是新的攻擊面**（金鑰與提示詞送往該位址）→ production 拒絕、只允許 https、不得含帳密、值不回顯於錯誤訊息；壓測服務只用假金鑰。
- **ngrok 暴露開發環境** → 文件要求通道端加存取控制；開發用 OAuth 用戶端不用於雲端。
- **使用者不在場時雲端步驟會停住** → 狀態記在驗收紀錄，下次接續；不在背景自動繼續。

## Migration Plan

沒有資料表變動、沒有 migration。程式變更都是新增或限於 development／staging 的設定，對既有環境相容。部署：合併後照常；雲端資源由腳本建立，順序與回復見營運文件（映像回到前一版即可，資料庫結構不需回復）。若某一步失敗：停止，不做半套部署；資料庫尚為舊結構時舊版程式照常運作。

## Open Questions

- 資料庫的版本、規格與區域、寄信服務的供應商與帳密、預備環境的 OAuth 用戶端——這些是使用者要在實作第一步提供的輸入（D2），不改變規格與方法。
- 憑證驗證最終採用 D3 的哪一種，由探測決定；結果會寫進營運文件。
