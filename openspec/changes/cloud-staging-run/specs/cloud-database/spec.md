# Spec Delta

## Purpose

定義託管 SQL Server 如何安全且在使用者明確承擔費用的前提下建立與設定：只走私有連線、應用程式用獨立且權限受限的登入、連線必須驗證伺服器憑證、備份與時間點還原符合規範、日誌保存期限到位，並且每一項設定都可以事後驗證。

## ADDED Requirements

### Requirement: 建立資料庫執行個體前必須明確承擔費用
部署腳本 SHALL NOT 為資料庫的版本（edition）、規格（tier）、儲存容量與區域提供預設值；這些 SHALL 由呼叫者明確提供。產生「建立執行個體」指令的步驟 SHALL 需要明確的費用確認參數，缺少時 SHALL 拒絕產生該步驟並說明這會產生固定的月費。預覽 SHALL 顯示建立執行個體是持續計費的資源，並列出實際會用到的版本、規格與區域。

#### Scenario: 缺少費用確認
- **WHEN** 要求資料庫步驟組但沒有提供費用確認參數
- **THEN** 腳本以非零代碼結束並說明會產生固定月費，沒有輸出建立執行個體的指令

#### Scenario: 缺少規格
- **WHEN** 沒有提供版本、規格、儲存容量或區域任一項
- **THEN** 腳本拒絕並指出缺少的參數名稱，不使用任何預設值

#### Scenario: 預覽顯示計費資源
- **WHEN** 提供全部參數與費用確認並預覽
- **THEN** 輸出標明持續計費，並列出版本、規格、儲存容量與區域

### Requirement: 資料庫只走私有連線並使用獨立的受限登入
資料庫執行個體 SHALL NOT 具有公開 IP，應用程式 SHALL 經私有 IP 連線。應用程式 SHALL 使用專用的登入，SHALL NOT 使用 `sa` 或其他系統管理員登入；該登入只對自己的資料庫有執行 migration 與讀寫所需的權限。staging、壓測環境與 production SHALL 使用各自獨立的資料庫與登入，SHALL NOT 共用。登入的密碼 SHALL 只存在於 Secret Manager，SHALL NOT 出現在指令參數、輸出或日誌。

#### Scenario: 沒有公開 IP
- **WHEN** 檢視建立執行個體的指令
- **THEN** 明確關閉公開 IP 並指定私有網路

#### Scenario: 不用系統管理員登入
- **WHEN** 檢視建立登入與連線字串的步驟
- **THEN** 登入名稱不是 `sa`，且權限限於該環境自己的資料庫

#### Scenario: 環境互相獨立
- **WHEN** 以 staging 與壓測環境各預覽一次
- **THEN** 資料庫名稱、登入名稱與機密名稱沒有任何相同者

### Requirement: 連線必須驗證伺服器憑證
staging 與 production 的資料庫連線 SHALL 啟用加密並驗證伺服器憑證，SHALL NOT 使用 `TrustServerCertificate=yes`，也 SHALL NOT 以此作為無法驗證時的退路。部署流程 SHALL 包含一個實際連線測試（由雲端工作從私有網路連到資料庫並完成加密連線與憑證驗證）；測試失敗時 SHALL 停止，不繼續 migration 與部署。

#### Scenario: 憑證驗證成功
- **WHEN** 雲端工作以含憑證驗證的連線字串連到資料庫
- **THEN** 連線成功，`/readyz` 回 200

#### Scenario: 禁用不安全的退路
- **WHEN** 預備或正式環境的連線字串含 `TrustServerCertificate=yes`
- **THEN** 應用程式啟動失敗（沿用既有規則），部署流程不提供繞過的選項

#### Scenario: 無法驗證時停止
- **WHEN** 連線測試因憑證無法驗證而失敗
- **THEN** 流程停止並回報，不執行 migration

### Requirement: 自動備份與時間點還原
資料庫執行個體 SHALL 啟用自動備份與時間點還原（含交易記錄），保留至少 30 份備份；備份 SHALL 存放於與執行個體不同的區域，且 SHALL 加密（至少為平台預設的靜態加密）。系統 SHALL 能以唯讀指令驗證這些設定確實生效，驗證結果 SHALL 記錄在驗收紀錄。

#### Scenario: 設定生效
- **WHEN** 建立執行個體並套用備份設定後，以唯讀指令檢視
- **THEN** 自動備份與時間點還原為開啟、保留備份數不少於 30、備份位置與執行個體的區域不同

#### Scenario: 備份位置必須明確
- **WHEN** 沒有提供備份位置
- **THEN** 腳本拒絕，不以執行個體的區域作為預設

### Requirement: 應用程式日誌保存 30 天
應用程式日誌 SHALL 保存 30 天（AGENTS.md 第 11 節）；部署腳本 SHALL 設定日誌儲存桶的保存期限為 30 天，並能以唯讀指令驗證。

#### Scenario: 保存期限
- **WHEN** 套用日誌設定後以唯讀指令檢視
- **THEN** 保存期限為 30 天
