# load-testing Specification

## Purpose
定義負載測試如何在不消耗模型額度、不危及正式環境的前提下量測容量：以可設定延遲與錯誤率的假模型取代真實模型，依既定的使用者行為與通過標準判定結果，並保證這個測試入口不可能在 staging 或 production 啟動。

## Requirements

### Requirement: 使用者行為情境
負載測試 SHALL 模擬完整的會員行為：註冊（含年齡確認與條款同意）與登入、開新對話、每 30 至 45 秒送出一則訊息，並混入讀取對話列表與以 `since_seq` 增量拉取。虛擬使用者數、持續時間與加壓方式 SHALL 可由參數指定，預設為 100 位虛擬使用者、持續 15 分鐘。每位虛擬使用者 SHALL 使用自己的帳號，且 SHALL 通過 CSRF 檢查。

#### Scenario: 一位虛擬使用者的流程
- **WHEN** 一位虛擬使用者開始
- **THEN** 依序完成註冊、登入、開新對話，之後週期性送出訊息並收到回覆

#### Scenario: 參數
- **WHEN** 指定 20 位虛擬使用者與 1 分鐘
- **THEN** 測試依此規模執行

### Requirement: 假模型入口只在開發與測試載入
負載測試 SHALL 使用以假模型取代真實供應商的應用程式入口，假模型 SHALL 可設定回應延遲與各類錯誤（速率限制、逾時、伺服器錯誤）的比率。這個入口 SHALL 在 `APP_ENV` 為 `staging` 或 `production` 時拒絕啟動並以非零代碼結束，且 SHALL NOT 被容器映像包含。

#### Scenario: 正式環境拒絕
- **WHEN** 以 `APP_ENV=production` 載入假模型入口
- **THEN** 啟動失敗，訊息說明此入口不可用於該環境

#### Scenario: 預備環境拒絕
- **WHEN** 以 `APP_ENV=staging` 載入假模型入口
- **THEN** 啟動失敗

#### Scenario: 不在映像內
- **WHEN** 檢視容器映像的內容清單
- **THEN** 沒有負載測試的檔案

#### Scenario: 設定錯誤率
- **WHEN** 假模型設定 10% 逾時
- **THEN** 約 10% 的模型呼叫逾時，其餘依設定延遲後回覆

### Requirement: 通過標準由腳本判定
負載測試 SHALL 提供判定腳本，讀取測試結果並以建議的標準判定通過與否：端到端 p95 小於 8 秒、錯誤率小於 1%、日誌中沒有資料庫鎖定錯誤。CPU 使用率低於 70% SHALL 由平台量測並由操作者填入，腳本 SHALL 在未提供時把該項標為「未驗證」而非通過。結果 SHALL 只輸出彙總數字，SHALL NOT 含使用者資料。

#### Scenario: 全部達標
- **WHEN** 結果 p95 為 3 秒、錯誤率 0.2%、沒有鎖定錯誤，且提供 CPU 為 55%
- **THEN** 判定通過

#### Scenario: p95 超過
- **WHEN** 結果 p95 為 9 秒
- **THEN** 判定未通過，指出是 p95

#### Scenario: CPU 未提供
- **WHEN** 其餘項目達標但沒有提供 CPU
- **THEN** CPU 標為未驗證，整體不判為通過

#### Scenario: 資料庫鎖定
- **WHEN** 日誌出現資料庫鎖定錯誤
- **THEN** 判定未通過

### Requirement: 只對測試或預備環境執行
負載測試 SHALL 預設只對本機位址執行；對其他位址執行 SHALL 需要明確的參數確認，且 SHALL 拒絕對文件所列的正式環境網域執行。測試產生的帳號 SHALL 使用保留的測試網域信箱，SHALL NOT 寄出任何真實信件。

#### Scenario: 預設本機
- **WHEN** 不指定目標位址
- **THEN** 對本機位址執行

#### Scenario: 其他位址需確認
- **WHEN** 指定非本機位址而沒有確認參數
- **THEN** 測試拒絕執行

#### Scenario: 正式網域
- **WHEN** 指定的位址是正式環境網域
- **THEN** 即使有確認參數也拒絕執行

#### Scenario: 測試帳號
- **WHEN** 虛擬使用者註冊
- **THEN** 信箱屬於保留的測試網域
