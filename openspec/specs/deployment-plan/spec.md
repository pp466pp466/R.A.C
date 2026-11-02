# deployment-plan Specification

## Purpose
定義雲端部署如何以可檢視、可測試的方式寫進程式庫：機密與一般設定如何對應到平台、migration 如何作為先於新版程式的獨立步驟、排程指令何時執行，並保證預設只印出將執行的指令、計畫本身不含任何機密值。

## Requirements

### Requirement: 部署腳本預設只預覽
部署腳本 SHALL 預設只印出將要執行的完整指令（預覽），SHALL NOT 在沒有明確的執行參數時呼叫任何雲端指令。使用執行參數時，系統 SHALL 在每一步執行前列出該步驟並要求確認；專案識別碼、區域與環境名稱 SHALL 由呼叫者明確提供，SHALL NOT 有預設的專案。

#### Scenario: 預設預覽
- **WHEN** 不帶執行參數執行任一子命令
- **THEN** 輸出將執行的指令，沒有呼叫任何外部指令

#### Scenario: 缺少專案
- **WHEN** 沒有提供專案識別碼
- **THEN** 腳本以非零代碼結束並說明缺少的參數，不輸出任何指令

#### Scenario: 逐步確認
- **WHEN** 使用執行參數
- **THEN** 每一步執行前列出該步驟並等待確認；拒絕則停止，不執行後續步驟

### Requirement: 設定的對應完整且不含機密值
部署計畫 SHALL 把每一個應用程式讀取的環境變數歸類為「機密」或「一般設定」：機密 SHALL 由 Secret Manager 注入，一般設定 SHALL 以環境變數明文設定。計畫 SHALL 涵蓋 `.env.example` 列出的全部正式環境變數，不得有未歸類的變數。計畫與腳本輸出 SHALL NOT 含任何機密的值，也 SHALL NOT 要求機密以命令列參數傳遞；`TEST_SQLSERVER_URL` 與僅限開發的變數 SHALL NOT 出現在正式環境的設定中。

#### Scenario: 全部變數都有歸類
- **WHEN** 比對 `.env.example` 的變數與部署計畫
- **THEN** 每一個變數恰好出現在機密、一般設定或「不部署」其中一類

#### Scenario: 機密只以名稱出現
- **WHEN** 檢視預覽輸出
- **THEN** 機密只以名稱與 Secret Manager 版本參照出現，沒有任何值

#### Scenario: 建立機密不經命令列
- **WHEN** 預覽建立機密的步驟
- **THEN** 機密的值來自標準輸入或檔案，不在指令的參數中

#### Scenario: 開發專用變數不部署
- **WHEN** 檢視正式環境的設定
- **THEN** 沒有 `TEST_SQLSERVER_URL` 與 `FLASK_DEBUG`，且 `APP_ENV` 為該環境的名稱

### Requirement: migration 是先於新版程式的獨立步驟
部署計畫 SHALL 把資料庫 migration 定義為獨立的工作（執行 `alembic upgrade head`），且在計畫中 SHALL 排在新版服務部署之前；SHALL 在 migration 前包含備份的步驟。應用程式 SHALL NOT 在啟動時自動執行 migration。

#### Scenario: 順序
- **WHEN** 列出完整計畫的步驟順序
- **THEN** 備份在 migration 之前，migration 在部署服務之前

#### Scenario: 失敗即停止
- **WHEN** migration 工作失敗
- **THEN** 計畫不繼續部署新版服務

### Requirement: 排程指令的頻率與失敗處理
部署計畫 SHALL 為下列指令各建立一個排程工作：`check-alerts` 每 5 分鐘、`execute-account-deletions`、`purge-expired-data` 與 `purge-inactive-accounts` 各每日一次（台灣時間凌晨，錯開不同時間）。每個排程工作 SHALL 使用與服務相同的映像與機密、以單次執行的方式運行，且指令以非零代碼結束時 SHALL 被視為失敗。計畫中的排程清單 SHALL 與應用程式實際提供的排程類指令一致：新增排程類指令而未列入計畫時，測試 SHALL 失敗。

#### Scenario: 四個排程
- **WHEN** 預覽排程步驟
- **THEN** 輸出四個排程工作及其頻率，`check-alerts` 為每 5 分鐘

#### Scenario: 與指令一致
- **WHEN** 應用程式新增一個排程類指令但沒有列入計畫
- **THEN** 一致性測試失敗

#### Scenario: 失敗被視為失敗
- **WHEN** 某個排程指令以非零代碼結束
- **THEN** 該次工作執行狀態為失敗（由平台告警接手，見 platform-alerts）

### Requirement: 預備與正式環境互相隔離
部署計畫 SHALL 以環境名稱區分所有資源的名稱（服務、工作、機密、排程），`staging` 與 `production` SHALL 使用各自獨立的資料庫與機密，SHALL NOT 共用。計畫 SHALL NOT 把正式環境的資料複製到預備環境。

#### Scenario: 名稱帶環境
- **WHEN** 以 `staging` 與 `production` 各預覽一次
- **THEN** 兩次輸出沒有任何相同的資源名稱
