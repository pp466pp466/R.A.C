# aws-demo-environment Specification

## Purpose
定義 AWS 展示環境的整體拓撲、區域與費用護欄，以及佈建腳本如何以「預設預覽、逐步確認、可整批拆除」的方式建立與清除資源；並聲明它是 `staging` 的展示部署，不是 production，也不是容量依據。

## Requirements

### Requirement: 展示環境是 staging 且只在雪梨區域
展示環境 SHALL 以 `APP_ENV=staging` 運行，SHALL NOT 放寬任何 staging 的啟動檢查（機密缺漏即失敗、不得使用 `sa` 與 `TrustServerCertificate`、不得設定 `TRUST_PROXY_HEADERS`）。所有資源 SHALL 位於 `ap-southeast-2`；佈建腳本在區域不是 `ap-southeast-2` 時 SHALL 拒絕執行。

#### Scenario: 區域守門
- **WHEN** 以 `ap-northeast-1` 執行佈建腳本
- **THEN** 腳本拒絕並說明只接受雪梨區域，沒有呼叫任何 AWS 指令

#### Scenario: 設定驗證不放寬
- **WHEN** 展示環境的容器啟動時缺少一項必要機密
- **THEN** 容器以非零碼結束，訊息只列出變數名稱

### Requirement: 拓撲固定為單一主機加託管資料庫
展示環境 SHALL 由一台 EC2 `t3.small`（Elastic IP、不開放 SSH、以 SSM Session Manager 管理）與一個 RDS for SQL Server Express `db.t3.micro` 組成；EC2 上運行客服池、RPG 串流池與反向代理。腳本 SHALL NOT 建立其他計費服務（例如負載平衡器、NAT 閘道、第二台主機），除非本規格被修訂。

#### Scenario: 預覽列出全部資源
- **WHEN** 預覽完整計畫
- **THEN** 建立的資源只有：安全群組、IAM 角色與執行個體設定檔、EC2 主機與其磁碟、Elastic IP、資料庫子網路群組、RDS 執行個體、SSM 參數、預算與狀態告警所需的 SNS 主題與 CloudWatch 告警

#### Scenario: 沒有 SSH
- **WHEN** 檢視主機的安全群組規則
- **THEN** 對外只開放 80 與 443，沒有 22

### Requirement: 資源一律加標籤並可整批拆除
腳本建立的每一個資源 SHALL 帶有標記專案與環境的標籤。腳本 SHALL 提供 `teardown`：預設只預覽將刪除的資源清單，必須經逐步確認才刪除，且只刪除帶有該標籤的資源；RDS 刪除前 SHALL 要求使用者明確選擇是否保留最終快照。不論選擇為何，`teardown` SHALL 先列出該資料庫仍存在的部署前快照，並要求使用者明確回覆要保留的例外（「沒有例外」也要明說）；沒有被指定保留的部署前快照才列入刪除，且 SHALL 只刪除帶有本專案標籤、名稱屬於本資料庫的部署前快照。最終快照（本次保留的，或先前拆除留下的）SHALL NOT 被 `teardown` 刪除。

#### Scenario: 只動有標籤的資源
- **WHEN** 預覽拆除計畫
- **THEN** 清單只含帶有本專案標籤的資源，不含帳號中的其他資源

#### Scenario: 刪除資料庫前確認快照
- **WHEN** 拆除計畫包含 RDS 執行個體
- **THEN** 計畫要求使用者明確選擇是否保留最終快照，沒有預設值

#### Scenario: 列出部署前快照並由使用者指定例外
- **WHEN** 預覽拆除計畫（不論最終快照的選擇）
- **THEN** 腳本列出仍存在的部署前快照；使用者明確回覆要保留的例外（或沒有例外）之前，計畫不含任何刪除快照的指令；回覆之後只有未被指定保留的快照列入刪除

#### Scenario: 被指定保留的快照不被刪除
- **WHEN** 使用者把某個部署前快照指定為保留
- **THEN** 拆除計畫不含刪除它的指令，拆除後它仍在

#### Scenario: 最終快照不被拆除流程刪除
- **WHEN** 使用者選擇保留最終快照，或資料庫旁有先前拆除留下的最終快照
- **THEN** 計畫沒有任何刪除最終快照的指令

#### Scenario: 只刪本專案的部署前快照
- **WHEN** 要刪除的名單含名稱不屬於本資料庫、不是部署前快照，或不帶本專案標籤的快照
- **THEN** 計畫拒絕名稱不符者、略過沒有標籤者，不刪除它們

### Requirement: 費用護欄先於計費資源
建立任何計費資源前，腳本 SHALL 顯示資源清單與預估的每日與到 2026-12-31 的費用範圍並要求 `--accept-cost`；沒有該參數時 SHALL NOT 產生建立計費資源的指令。腳本 SHALL 建立預算告警（以電子郵件通知），預估與預算 SHALL 標明價格會變、以 AWS 帳單頁為準。

#### Scenario: 沒有同意費用
- **WHEN** 不帶 `--accept-cost` 預覽建立 RDS 與 EC2 的步驟
- **THEN** 輸出說明需要 `--accept-cost`，且不輸出任何建立計費資源的指令

#### Scenario: 預算告警
- **WHEN** 預覽預算步驟
- **THEN** 輸出至少兩個門檻的預算告警與電子郵件通知對象，通知對象由呼叫者提供且沒有預設值

### Requirement: 佈建腳本預設預覽並逐步確認
佈建腳本 SHALL 預設只印出將執行的指令，不得呼叫任何 AWS 指令；使用 `--execute` 時每一步執行前 SHALL 列出該步驟並等待確認，拒絕即停止；`--only STEP --yes` SHALL 只執行指定的單一步驟，確認由呼叫者針對該步驟明確給出。腳本輸出與預覽 SHALL NOT 含任何機密值，機密 SHALL NOT 出現在命令列參數。

#### Scenario: 預設預覽
- **WHEN** 不帶執行參數執行任一子命令
- **THEN** 輸出將執行的指令，沒有呼叫任何外部指令

#### Scenario: 單步執行
- **WHEN** 以 `--only STEP --yes` 執行
- **THEN** 只執行該步驟，不執行相鄰步驟

#### Scenario: 機密不經命令列
- **WHEN** 檢視任何步驟的預覽
- **THEN** 機密只以名稱出現，值來自標準輸入或檔案

### Requirement: 展示環境不得被當成 production 或容量依據
展示環境的主機名稱與資源名稱 SHALL 不含 `production`。營運文件 SHALL 載明：它不是 production、不含真實會員資料、容量數字不是依據，且備份保存、日誌保存與平台告警未達 production 要求。

#### Scenario: 名稱守門
- **WHEN** 檢視所有資源名稱與網域
- **THEN** 沒有任何一個含 `production`

#### Scenario: 文件載明限制
- **WHEN** 檢視營運文件的展示環境章節
- **THEN** 載明上述限制與每一項未達標的缺口
