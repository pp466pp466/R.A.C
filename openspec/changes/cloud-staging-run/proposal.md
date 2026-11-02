# Proposal

## Why

`staging-deployment`（C-5b）把預備環境的應用程式側行為在本機驗證到底，並備好了雲端需要的程式（`GEMINI_BASE_URL`、假模型伺服器、階梯式加壓、冒煙驗收腳本）。剩下的是**只能在真實的 Google Cloud 上驗證的部分**：C-5a 的 `gcloud` 腳本與平台告警定義從未對真實環境執行過；Cloud SQL for SQL Server 的私有連線與伺服器憑證驗證沒有人試過；容量數字、gunicorn 與 Cloud Run 的尺寸、平台告警是否真的會寄信、還原演練的耗時，全都只有假設值或本機數字。上線前關卡裡「負載測試」「託管資料庫」兩項只能在這裡完成。

**前提：使用者要有一個可計費的 Google Cloud 專案**（已啟用計費、安裝並登入 `gcloud`、看過計價頁並同意 Cloud SQL 的固定月費）。使用者目前沒有，所以本 change 先規劃好、不實作，等前提具備再做。production 與對外開放仍順延（C-5d）。

## What Changes

- **雲端資料庫（預設只預覽，需明確承擔費用）**：`gcp_launch.py` 新增 `database` 組——建立 Cloud SQL for SQL Server 執行個體（私有 IP、無公開 IP）、應用程式專用的登入（不用 `sa`）、各環境獨立的資料庫、自動備份加時間點還原（備份存放於不同區域）、應用程式日誌保存 30 天。**版本（edition）、規格（tier）、儲存容量與區域沒有預設值**，且需要 `--accept-cost` 才會產生建立執行個體的指令。連線必須**驗證伺服器憑證**（不得使用 `TrustServerCertificate=yes`），無法驗證時停止。
- **預備環境實際建立與驗證**：依序執行 `secrets` → `database` → `build` → `migrate` → `deploy` → `schedule` → `alerts`，每一步前確認；發現的 `gcloud` 差異回寫到 `scripts/launch_plan.py` 並補測試。冒煙腳本對 Cloud Run 網址再跑一次。
- **單步執行**：我的工具沒有互動式輸入，所以新增 `--only STEP_ID --yes`（只執行一個步驟，確認由使用者在對話中針對該步驟給出）；資料庫登入的密碼提示必須在使用者自己的終端機執行。
- **預備環境的管理員**：`create-admin` 新增 `--password-env NAME`（從環境變數讀密碼，供 Cloud Run 工作以機密注入；與 `--password-stdin` 互斥）；計畫新增一次性的 `admin` 組，用完即刪除機密與工作。
- **平台告警演練**：六類告警（5xx、存活、就緒、寄信失敗、排程失敗、告警檢查停擺）各在預備環境觸發一次並記錄證據，演練可逆。
- **隔離的壓測環境**：同一個映像另部署 `staging-loadtest` 服務（自己的資料庫、機密與假金鑰），`GEMINI_BASE_URL` 指向雲端上的假模型伺服器（`loadtest/fake_llm/`，由 `staging-deployment` 提供）。壓測目標必須帶壓測標記。
- **容量量測與 gunicorn 調整**：CPU 使用率從 Cloud Monitoring 取得餵給 `evaluate.py`；依實測調整 `WEB_CONCURRENCY`、`GUNICORN_THREADS` 與 Cloud Run 的 CPU／記憶體／並行數（計畫新增這些覆寫），每一輪記錄在案。
- **雲端還原演練**：`restore-drill` 組以 `gcloud sql instances clone`（時間點）複製出名稱帶 `_restore_drill` 的執行個體，用 Cloud Run 工作在複製上執行 `replay-deletions`（預覽、實際、再一次），驗證後刪除；量測還原耗時。來源只讀。
- **依使用者決定，不在本 change**：production 資源與對外開放（C-5d）；自有網域與信件的 SPF／DKIM／DMARC；入口層 IP 限流；法務覆核與模型供應商方案。

## Capabilities

### New Capabilities
- `cloud-database`: 託管 SQL Server 的建立（費用確認、私有連線）、獨立登入、伺服器憑證驗證、自動備份與時間點還原、日誌保存期限。
- `cloud-restore-drill`: 以 Cloud SQL 複製執行個體演練還原後重新抹除，來源只讀、只對暫存名稱的複製動手、量測耗時。

### Modified Capabilities
- `deployment-plan`: 新增 `database`、`admin`、`loadtest`、`restore-drill` 步驟組、單步執行、資源尺寸覆寫。
- `load-testing`: 預備環境壓測使用隔離的壓測服務、CPU 由 Cloud Monitoring 取得。
- `platform-alerts`: 六類告警各在預備環境演練並留下證據。
- `access-control`: `create-admin` 可從機密注入的環境變數讀取密碼（非互動的雲端工作）。

## Impact

- **修改**：`scripts/launch_plan.py`（`database`／`admin`／`loadtest`／`restore-drill` 組、尺寸覆寫、檔案掛載的機密、互動步驟）、`scripts/gcp_launch.py`（`--only`／`--yes`、由部分組成的機密）、`app/cli.py`（`--password-env`）、`loadtest/guard.py`（壓測標記）、`loadtest/evaluate.py`、`gunicorn.conf.py`（數值依實測）；新增 `loadtest/cloud_cpu.py`。
- **文件**：營運與部署（預備環境建置紀錄、壓測與調整紀錄、還原演練、告警演練、關卡狀態）、README 驗收紀錄、路線圖。
- **風險**：①**會產生雲端費用**（Cloud SQL 為固定月費，另有 Cloud Run、Cloud Build、Cloud Monitoring）——每個產生費用的步驟前顯示並確認，執行個體建立需要 `--accept-cost`；②Cloud SQL for SQL Server 的伺服器憑證能否在 ODBC Driver 18 驗證（私有 IP 沒有主機名稱）**尚未驗證**，是第一個要做的探測，失敗就停止而不是退而求其次用 `TrustServerCertificate=yes`；③預備環境是公開的 `*.run.app`，以每日用量上限與最大執行個體數限制被濫用的損失；④`gcloud` 與 Cloud Monitoring 的旗標與 JSON 在 C-5a 無法驗證，預期會有不少回寫修正。
