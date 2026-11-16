# Proposal

## Why

展示站目前是 Render 上的應用程式接 Azure 的免費 SQL 資料庫，而 Azure 的免費帳戶點數約 **2026-12-08** 到期，早於展示需要撐到的 **2026-12-31**；使用者另外沒有可計費的 Google Cloud 專案（`cloud-staging-run` 因此擱置）。使用者的 AWS 新帳號有 US$100 的免費點數（到期 2027-05-11），所以展示環境改搬到 AWS。這個環境同時是 `rpg-ops-loadtest`（壓測與營運調整）要量測的真實基礎，因此要先把它建起來、部署到 migration 0018、能用真實 Google 登入，而且全程不放寬任何 staging 的安全規則。

## What Changes

- **展示環境的拓撲**（只用雪梨 `ap-southeast-2`；帳號是 Free plan，不升級）：一台 EC2 `t3.small`（Amazon Linux、不開 SSH，以 SSM Session Manager 管理、Elastic IP）跑應用程式映像的**兩個池**（客服池與 RPG 串流池，沿用 `gunicorn.conf.py` 與 `gunicorn.rpg.conf.py`）與 Caddy；一個 RDS for SQL Server Express `db.t3.micro`（不對外、只允許 EC2 的安全群組連線、加密、自動備份）。`APP_ENV=staging`，沒有任何放寬。
- **HTTPS 入口**：DuckDNS 子網域 `rac001.duckdns.org` 指向 Elastic IP，Caddy 取得並自動更新憑證、HTTP/2、把 `/api/game/` 分流到 RPG 池（關閉回應緩衝以保留 SSE），其餘到客服池；`BASE_URL` 設為該網址，並在 Google 的預備環境 OAuth 用戶端補上回呼網址（Google 是否接受免費子網域尚未驗證，ngrok 為備用）。
- **資料庫與憑證驗證**：連線**必須驗證 RDS 伺服器憑證**（不得 `TrustServerCertificate=yes`、不得用 `sa`）；以專屬的應用程式登入連線，結構由 Alembic 升到 `0018`（先備份、獨立步驟、先於新版程式），並以 `seed-scripts` 補內建劇本。展示環境從空資料庫開始，**不搬 Azure 的資料**。
- **機密與設定**：機密存於 SSM Parameter Store（SecureString）、以 EC2 角色在主機啟動時取出到記憶體檔案再交給容器；一般設定以環境變數。機密不經命令列、不進映像、不進日誌，與既有的 `deployment-plan` 規範一致（名稱分類沿用 `scripts/launch_plan.py` 的清單）。
- **排程**：`SCHEDULED_COMMANDS`（`app/cli.py`）的五個排程指令在主機上以 cron 觸發單次容器（台灣時間），清單與頻率由同一份資料產生，不另外手寫。
- **佈建腳本（預設預覽）**：新增 `scripts/aws_demo_plan.py`（純函式，產生步驟、Caddy 設定、systemd 單元與 cron 內容，可測試）與 `scripts/aws_demo.py`（執行器：預設只預覽、`--execute` 逐步確認、`--only STEP --yes` 單步執行、`--accept-cost` 才建立計費資源）；區域守門（只接受雪梨）、資源一律加標籤以便整批拆除，並提供 `teardown`。
- **費用護欄**：建立資源前顯示預估與使用者同意；AWS Budgets 預算告警；展示結束後的拆除步驟。
- **有限的監控**：`check-alerts` 排程（應用程式內的告警，沿用 Brevo 寄信）、EC2 狀態檢查告警；**5xx 比率、外部存活探測與寄信失敗等平台告警不做**，明列在上線前關卡的缺口。
- **驗收**：`scripts/staging_smoke.py` 對 `https://rac001.duckdns.org` 通過（模型呼叫除外），Google 登入由使用者人工驗收；結果與限制寫入驗收紀錄。
- **文件**：營運與部署文件新增「AWS 展示環境」章節與 Mermaid 拓撲圖；README 驗收紀錄；路線圖加上本 change，並說明 RC-4 拆成兩個 change。
- **不在本 change**：假 RPG 模型與 AGENTS.md 第 9 節修訂（http 回送位址）、同時串流上限加 503 加 `Retry-After`、斷線記錄欄位與遷移、Locust 串流情境、告警擴充、容量量測與校正——全部屬於後續的 `rpg-ops-loadtest`；Render 展示站與 Azure 資料庫不動（Azure 到期後 Render 站會失效，由使用者決定如何處理）；production 與對外開放；自有網域與信件的 SPF／DKIM／DMARC；金流。

## Capabilities

### New Capabilities
- `aws-demo-environment`: 展示環境的拓撲、區域與帳號限制、標籤與拆除、費用護欄、佈建腳本的預覽優先與逐步確認，以及它不是 production 的聲明。
- `aws-demo-database`: RDS SQL Server Express 的私有存取、專屬登入、伺服器憑證驗證、備份、migration 與管理員／內建劇本的初始化。
- `aws-demo-ingress`: DuckDNS 網域、Caddy 的 HTTPS 與 HTTP/2、客服池與 RPG 串流池的路徑分流（含串流不被緩衝）、Google 登入回呼網址。
- `aws-demo-operations`: 機密注入、容器啟動與重啟、排程指令、部署與回復、有限的告警，以及驗收與缺口的誠實揭露。

### Modified Capabilities

（無。既有的 `deployment-plan`、`container-runtime`、`launch-gates` 等針對 Google Cloud 與正式環境的需求不變；展示環境是它們之外的一個 staging 部署。）

## Impact

- **新增**：`scripts/aws_demo_plan.py`、`scripts/aws_demo.py`、`scripts/aws_demo_bootstrap_db.py`（在 EC2 上以主映像執行的一次性資料庫初始化）、`tests/test_aws_demo_plan.py` 與 `tests/test_aws_demo_script.py`。
- **文件**：`docs/營運與部署-operations.md`（新章節與拓撲圖、上線前關卡的缺口）、`docs/路線圖-roadmap.md`、README（驗收紀錄）、`.env.example` 只在需要新變數時更新；`tests/test_design_docs.py` 需通過。
- **不修改**：`app/` 底下的應用程式程式碼（預期不需要；若憑證驗證的探測發現必須改，先回頭修訂本 change 再動手）、`Dockerfile`、`gunicorn*.conf.py`、migration、AGENTS.md。
- **外部系統**：AWS 帳號（雪梨：EC2、RDS、EBS、Elastic IP、SSM、IAM 角色、安全群組、Budgets、CloudWatch、SNS）、DuckDNS、Let's Encrypt、Google OAuth 用戶端、Brevo SMTP、OpenRouter 與 Gemini。
- **風險**：①**費用**（抵用金 US$100：EC2、RDS Express、儲存與公有 IPv4 合計約 US$70 內，不含超額；拆除前持續計費）；②**RDS 伺服器憑證能否在 ODBC Driver 18 驗證**要先探測，失敗就停止，不退讓；③**Google 是否接受 `duckdns.org` 的回呼網址**尚未驗證；④**兩個 gunicorn 池加 Caddy 跑在 2 GB 記憶體的主機上**，要量測記憶體並備有交換檔；⑤展示環境**不是容量依據**也不符合 production 的備份、日誌保存與平台告警要求，缺口明列。
- **前提與假設**：AWS CLI 已授權（`aws login --region ap-southeast-2`）；DuckDNS 的 token 與子網域已申請；本機沒有 Docker，映像在 EC2 上建置；Azure 資料不搬（要搬需另行決定）。
