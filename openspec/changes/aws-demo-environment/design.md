# Design

## Context

動機與範圍見 proposal.md。本 change 是 RC-4 的第一半（`rpg-ops-loadtest` 是第二半）。現況（已查證）：

- **平台**：AWS 帳號是 Free plan（2026-11 新開，抵用金 US$100、到期 2027-05-11），專案綁定雪梨 `ap-southeast-2`，組織政策拒絕其他區域；`aws login --region ap-southeast-2` 已授權，本機有 AWS CLI 2.40，**沒有 Docker**。Free plan 可用 EC2 規格含 `t3.small`；雪梨牌價約：t3.small 每小時 US$0.0264、RDS SQL Server Express `db.t3.micro` 每小時 US$0.031。
- **應用程式**：映像（`Dockerfile`）含 ODBC Driver 18、gunicorn 兩份設定（客服池、RPG 串流池，映像預設啟動客服池）；`staging` 的 `ProxyFix` 信任單層轉送標頭；`/api/game/` 的路徑分流在營運文件列為「待部署」；排程指令清單集中在 `app/cli.py` 的 `SCHEDULED_COMMANDS`（5 個，台灣時間）；`DATABASE_URL` 的驗證只擋 `TrustServerCertificate=yes` 與 `sa`，其餘查詢參數原樣交給 pyodbc。
- **既有的腳本慣例**：`scripts/launch_plan.py`（純函式的步驟資料，含機密／一般設定／不部署的變數歸類）與 `scripts/gcp_launch.py`（預設預覽、`--execute` 逐步確認），各有測試。GCP 的 `cloud-staging-run` 擱置不動。
- **展示站現況**：Render 的應用程式接 Azure 資料庫，不動；DuckDNS 子網域 `rac001` 已申請、目前指向使用者家裡的位址。

## Goals / Non-Goals

**Goals:**
- 以可預覽、可測試、可重複執行的腳本建出展示環境，並在 2026-12-08 之前讓它可用。
- 所有 staging 的安全規則原樣生效；憑證驗證失敗時停止，而不是放寬。
- 兩個 worker 池第一次真正分流部署，供 `rpg-ops-loadtest` 量測。
- 費用可預期、可一鍵拆除。

**Non-Goals:**
- 改應用程式碼、Dockerfile、gunicorn 設定或 AGENTS.md（假模型的 http 例外與 503／`Retry-After` 屬 `rpg-ops-loadtest`）。
- 高可用、自動擴縮、藍綠部署、CI/CD 管線（單機展示，部署由人手動觸發）。
- 達到 production 的備份、日誌保存與平台告警要求（缺口明列，見 D11）。
- 搬移 Azure 資料；處理 Render 站在 Azure 到期後的去向。

## Decisions

### D1：執行順序與「發現就回寫」

依序：前提確認 → 預算與標籤 → 網路與角色 → 機密 → RDS（等待約 15 至 25 分鐘，期間做 EC2）→ EC2 與 Elastic IP → 主機初始化 → 建置映像 → 資料庫初始化與**憑證探測** → 快照與 migration → `create-admin` → 啟動服務 → DuckDNS 與 Caddy → 冒煙 → 排程 → 告警 → Google 登入人工驗收 → 記錄。每一步：預覽 → 逐步確認執行 → 以唯讀指令驗證 → 記錄。AWS CLI 與主機指令若與計畫不同，**先修計畫與測試再繼續**。**主機初始化不預先寫成計畫步驟**（使用者決定，不先猜）：第一次雲端執行時在 SSM 上逐步撰寫並確認，再依這條「發現就回寫」的規則寫成計畫步驟與測試。**第一次部署分組執行**：資料庫初始化與探測在主映像內以一次性容器執行，所以建置映像先於它們；啟動服務要等 `create-admin` 之後（見 D9）。雲端執行需要使用者在場；不在場時停在上一個已完成的步驟，狀態記在 README 驗收紀錄。

### D2：計畫與執行器分離，沿用既有慣例

- `scripts/aws_demo_plan.py`：純函式。輸入 `Target`（區域、環境標籤、網域、通知信箱…）與一般設定，輸出 `Step` 清單（步驟代碼、說明、指令引數清單、是否計費、是否互動、驗證指令）以及要放到主機的檔案內容（Caddyfile、systemd 單元與計時器、機密取出腳本）。不呼叫任何外部程式、不含機密值。
- `scripts/aws_demo.py`：執行器。預設預覽；`--execute` 逐步確認；`--only STEP --yes`；`--accept-cost`；`teardown`。呼叫以引數清單（不用 shell 字串）。機密經暫存檔（權限限本人、用後刪除）或標準輸入，不進引數。
- **重用**：變數歸類（`SECRET_VARS`、`PLAIN_VARS`、`NOT_DEPLOYED`、`OBSOLETE_VARS`）從 `scripts/launch_plan.py` 匯入，不複製；因此新增環境變數時兩條部署路徑同時被既有的守門測試覆蓋。
- **替代方案**：直接寫一份手動 runbook——拒絕，這個專案的部署慣例是可測試的計畫，且 apply 階段的工具沒有互動式輸入，單步執行加預覽比貼指令更安全。用 Terraform／CDK——拒絕，單機單資料庫的一次性展示用不上狀態管理，也增加一個要學的工具。

### D3：運算——單一 t3.small、不開 SSH

- Amazon Linux 2023、gp3 20 GB、IMDSv2 強制、啟動時加 2 GB 交換檔、`docker` 與 `aws` CLI 由系統提供或安裝。無金鑰對、無 22；管理用 SSM Session Manager（角色含受管政策 `AmazonSSMManagedInstanceCore` 加讀取機密路徑的內嵌政策）。
- 記憶體粗估（待 D12 量測）：兩個池各 2 個 worker（preload）約 0.7 GB、Caddy 約 50 MB、系統與 Docker 約 0.3 GB，餘約 0.9 GB；交換檔作安全網。若量測偏高，展示環境把 `WEB_CONCURRENCY` 降為 1，只改環境變數。
- **替代方案**：t3.medium（4 GB，每小時 0.0528，約 2 倍）——先不用，量測不夠再升；ECS Fargate／App Runner——Free plan 的可用性與費用不確定，且 RPG 長連線與兩池分流在單機更直接。

### D4：映像在主機上建置，來源經 S3 傳遞

本機沒有 Docker。流程：本機 `git archive HEAD`（只含已提交內容，天然排除 `.env`）→ 上傳到專屬的 S3 儲存貯體（加密、封鎖公開、帶標籤）→ 主機用角色取下並 `docker build`，標記 `rac-app:<提交短碼>`，保留前一版標籤。S3 用於傳檔，量極小。
- **替代方案**：ECR——多一個服務與權限，且 Free plan 儲存額度需另查；`scp`／SSM 直傳——SSM 命令有 size 上限，壓縮檔會超過。

### D5：兩個池與入口都是容器，由 systemd 管理；入口只信任單層

- 三個單元：`rac-cs`（`gunicorn.conf.py`，發布 `127.0.0.1:8081→8080`）、`rac-rpg`（`gunicorn.rpg.conf.py`，`127.0.0.1:8082→8080`）、`rac-caddy`（官方 Caddy 映像，host 網路，監聽 80／443，憑證與帳戶資料掛 `/var/lib/rac/caddy`）。單元 `Restart=always`、`--restart` 不由 Docker 負責（單一管理者）、`--env-file /run/rac/app.env`、`--user` 沿用映像的非 root、`--read-only` 加 `--tmpfs /dev/shm` 與必要的 tmpfs、`--cap-drop=ALL`、`--security-opt no-new-privileges`。
- Caddyfile：
  ```
  rac001.duckdns.org {
      @game path /api/game/*
      handle @game {
          reverse_proxy 127.0.0.1:8082 { flush_interval -1 }
      }
      handle {
          reverse_proxy 127.0.0.1:8081
      }
  }
  ```
  `flush_interval -1` 讓 SSE 立即送出；不對串流路徑加 `encode`（壓縮會緩衝）；Caddy 預設不對上游回應設逾時，不會切斷長串流。Caddy 預設送 `X-Forwarded-For／Proto／Host`，與 `ProxyFix(x_for=1, x_proto=1, x_host=1)` 相符；應用程式埠只綁回送位址，外部無法繞過入口（D3 的安全群組也只開 80／443）。
- **替代方案**：ALB——每月固定費用且需第二個可用區；Nginx——要自己處理 ACME；Cloudflare 隧道——引入第三方依賴。選 Caddy 因為憑證自動化與 HTTP/2 零設定。

### D6：機密存 SSM Parameter Store，啟動時取到記憶體

- 參數路徑 `/rac-demo/staging/<變數名稱>`，類型 SecureString（標準層，無費用）。主機的 `rac-render-env`（由計畫產生，ExecStartPre 執行）用角色取出全部參數，寫到 `/run/rac/app.env`（tmpfs、`0600`、root 擁有，由單元讀取後交給 `--env-file`）。換機密只需更新參數並重啟單元。
- 值的來源：本機被忽略的檔案（`--secrets-file`，不進版本庫）；`SECRET_KEY` 若不存在則隨機產生（至少 64 個十六進位字元）；`DATABASE_URL` 與 RDS 主要使用者密碼由腳本產生並直接寫入參數，**不顯示、不寫入檔案**。DuckDNS 權杖同為機密，用在執行器本機的 HTTP 呼叫，不經命令列（執行器用標準函式庫而非 `curl`）。
- 與既有規範對照：AGENTS.md 第 9 節「機密採同一套注入機制」在 AWS 的對應是平台機密儲存加執行時注入；不寫入映像、不寫入持久磁碟、不進日誌。
- **替代方案**：Secrets Manager——每個機密每月 US$0.40，十餘個機密約 US$5，且無額外功能需求；`.env` 檔放主機磁碟——違反「不落地」。

### D7：資料庫憑證驗證——先探測，不退讓

RDS 的伺服器憑證由 AWS 的 RDS CA 簽發，不在 Debian 的系統憑證庫內；與 Cloud SQL 不同的是 RDS 端點的主機名稱與憑證相符，所以只缺信任錨。候選做法依序嘗試：
1. **A（預設）**：從 AWS 公開的信任套件取得 RDS 的 CA 套件放在主機，唯讀掛進容器，連線字串加 `Encrypt=yes` 與 ODBC Driver 18 的 `ServerCertificate=<路徑>`，**只作用於資料庫連線**；探測以應用程式的連線設定（同一個 `DATABASE_URL`）執行 `SELECT 1`。**已知不確定**：`ServerCertificate` 在 Driver 18 的比對語意（與哪一層憑證比對、是否接受 CA 套件）需實測確認。
2. **B（A 失敗才用）**：在 `Dockerfile` 把 RDS 的 CA 加進系統憑證庫（只新增一個只簽發 RDS 端點的信任錨，不關閉任何驗證）。這會修改 Dockerfile 與映像，所以**選 B 之前必須先回頭修訂本 change（design、tasks，必要時 `container-runtime`），再動手**。
3. **禁止**：`TrustServerCertificate=yes`、`SSL_CERT_FILE` 之類改變整個行程信任範圍的做法（會影響模型供應商、SMTP、Google 的 TLS 驗證）。

探測（與資料庫初始化，D8）在主映像內以一次性容器執行，所以映像要先建置；在 migration 與啟動服務之前執行；兩種都失敗就停止回報使用者。結果（採用哪一種、連線字串型態）寫進營運文件。

### D8：資料庫初始化以主要使用者一次性執行，之後只用應用程式登入

- RDS 建立時主要使用者名稱不是 `sa`、密碼由腳本隨機產生並直接存入 SSM。
- `scripts/aws_demo_bootstrap_db.py` 在**主映像**內以一次性容器執行（腳本由主機掛入），用主要使用者連線：不存在才建立資料庫與專屬登入，並授予該資料庫的擁有者角色（遷移需要建表與改表）。可重複執行。它含 T-SQL 的 `CREATE LOGIN／DATABASE`，屬維運腳本，位於 `scripts/`，不在 `tests/architecture/test_no_raw_sql.py` 掃描的 `app/` 與 `migrations/`（與 `scripts/restore_drill.py` 的 `RESTORE` 同類）；腳本只做這幾個語句、不接受外部字串拼接（名稱經白名單驗證）。
- 之後 `DATABASE_URL` 一律是專屬登入（密碼另行隨機產生、URL 編碼）；主要使用者的密碼只留在 SSM 供日後重新初始化，應用程式容器不載入它（機密取出腳本依單元只取各自需要的參數：應用程式單元不含主要使用者密碼）。
- **替代方案**：應用程式直接用主要使用者——違反最小權限；初始化放進 Alembic——migration 不得含方言專屬原生 SQL（AGENTS.md 第 4 節）。

### D9：遷移前先做 RDS 快照；遷移是獨立的一次性容器

`aws rds create-db-snapshot`（等待可用）→ 一次性容器 `alembic upgrade head`（掛憑證、環境檔）→ 查 `alembic current` 等於最新版 → 才啟動／重啟兩個池。初次部署的快照是空資料庫，仍執行以驗證流程。遷移容器非零結束即停止。`seed-scripts` 在遷移後執行一次（冪等）。`create-admin` 是互動步驟：計畫列出要在 SSM 終端機執行的完整指令，密碼由使用者在該終端機輸入。**第一次部署分組執行**（`deploy_plan` 的 `groups`）：`build` → 資料庫初始化與探測（D7、D8）→ `snapshot`、`migrate`（含種子）→ `create-admin` → `restart`、`verify`；之後的例行部署一次跑完整份計畫（快照 → 建置 → 遷移 → 重啟 → 驗證）。

### D10：排程用 systemd 計時器，由 `SCHEDULED_COMMANDS` 產生

每個指令一對 `.service`（`docker run --rm`，同映像、同環境檔，`flask --app wsgi <cmd>`）與 `.timer`。把 cron 運算式轉成 `OnCalendar`：只支援清單實際用到的三種形式（`*/N * * * *`、`0 * * * *`、`M H * * *`），遇到其他形式即**丟錯**，使日後新增不支援的運算式時測試先失敗。時區用 `OnCalendar=… Asia/Taipei`。選 systemd 計時器而非 cron：失敗會留在 `systemctl --failed` 與 journald，且不必另裝 cron；`Persistent=true` 讓關機期間錯過的每日工作在開機後補跑。

### D11：監控只做三件事，其餘明列缺口

做：①`check-alerts` 計時器（應用程式內的 5 項告警，沿用 Brevo SMTP）；②CloudWatch 告警：EC2 `StatusCheckFailed`，通知到 SNS 主題的電子郵件訂閱（訂閱需要收件人點確認信，列為人工步驟）；③AWS Budgets：兩個門檻的預算告警。
不做（明列在營運文件為缺口，不假裝達標）：5xx 比率、外部存活與就緒探測（`/healthz`、`/readyz`）、寄信失敗與排程失敗的平台告警、備份保留 30 天與跨區域（RDS 只設 7 天、同區域）、應用程式日誌保存 30 天（只有 Docker 的循環日誌與 journald）。原因：展示用途、無真實會員、成本與時程；`rpg-ops-loadtest` 的告警擴充可再評估。

### D12：費用與容量的說法

- 預估（牌價，以帳單頁為準）：EC2 約 US$0.63／日、RDS 約 US$0.74／日、公有 IPv4 約 US$0.12／日、EBS 與 RDS 儲存約 US$0.15／日，合計約 US$1.6／日，到 2026-12-31（約 45 天）約 US$75，**在 US$100 抵用金內，餘裕不大**，所以預算告警設兩個門檻（預估金額的 60% 與 90%），並在過了展示期後執行 `teardown`。
- 容量：展示環境的併發、記憶體與延遲數字**不是容量依據**。冒煙驗收後記錄 `free -m` 與兩個池的常駐記憶體，供 `rpg-ops-loadtest` 校正 worker 與執行緒數。真實模型（OpenRouter、Gemini）只做連通性驗證，一般與成人模式不得用免費模型的既有規範不變。

### D13：與 AGENTS.md 的關係

AGENTS.md 第 9 節的「雲端平台為 Google Cloud」描述的是正式環境；展示環境是 `APP_ENV=staging`，用 AWS 並不違反任何一條可檢查的規則（HTTPS、HSTS、ProxyFix、非 root、不含 `.env`、機密不落地、migration 獨立步驟、兩個池）。本 change **不修改 AGENTS.md**；是否在第 9 節加一句「展示環境暫用 AWS，production 仍是 Google Cloud」留給使用者決定（見 Open Questions），並與 `rpg-ops-loadtest` 的 AGENTS.md 修訂一起處理。

## Risks / Trade-offs

- **費用超出抵用金** → `--accept-cost`、預算告警、D12 的估算、`teardown`；RDS 與 EC2 不在使用時（例如暫停展示）可停止，列在營運文件。
- **RDS 憑證驗證可能行不通** → D7 先探測、失敗即停止，並明訂 B 的前置修訂；不退讓到 `TrustServerCertificate=yes`。
- **Google 可能不接受 `duckdns.org` 的回呼網址** → 人工驗收時最先測；不接受則改用備用網域（另行決定，不放寬登入檢查）。
- **2 GB 記憶體不夠兩個池加 Caddy** → 交換檔、降 `WEB_CONCURRENCY`、必要時升 t3.medium；量測值記錄。
- **單機單點**（主機或 RDS 故障即停擺）→ 展示用途接受；RDS 自動備份與快照可還原，Elastic IP 可換主機。
- **Let's Encrypt 簽發失敗或被限流** → 憑證資料存持久路徑避免重簽；DuckDNS 是公共後綴下的子網域，額度按子網域計。
- **AWS CLI 與資源參數的細節在本機從未驗證** → D1 的回寫迴圈；預覽加測試先擋掉可預見的錯誤。
- **RDS 建立約 15 至 25 分鐘、快照與還原更久** → 把可並行的 EC2 步驟排在等待期間；腳本以輪詢等待並設上限。
- **Azure 到期後 Render 站失效** → 不在本 change；本環境上線後使用者可改以 AWS 網址展示。
- **展示環境暴露在網際網路** → 沒有真實會員資料、`.invalid` 測試帳號、全站每日用量上限設小值、管理員需 Google 加密碼；仍視為可能被掃描，不放敏感內容。

## Migration Plan

沒有資料表變動與 migration 檔；資料庫由空白升到現有的最新版。部署順序（第一次）：預算與標籤 → 網路與角色 → 機密 → RDS 與 EC2 → 主機初始化 → 建置 → 資料庫初始化與憑證探測 → 快照 → 遷移 → `create-admin` → 啟動 → 網域與入口 → 冒煙。失敗時：停止、不做半套（遷移之前的失敗只要重跑該步驟；遷移之後的服務異常以前一版映像標籤重啟，結構為 expand-only 不需回復）。拆除：`teardown` 預覽 → 逐步確認 → 驗證帶標籤的資源已清空。

## Open Questions

- 是否在 AGENTS.md 第 9 節加註展示環境暫用 AWS（見 D13）；與 `rpg-ops-loadtest` 的 AGENTS.md 修訂一併處理即可，不影響本 change 的實作。
- Azure 資料是否需要搬到 AWS（假設不搬：展示資料可重建）；若要搬，另開 change，不影響本 change 的任務。
- 展示期結束後 EC2 與 RDS 是停止還是 `teardown`（取決於是否還要保留環境給 `rpg-ops-loadtest` 後續使用）。
