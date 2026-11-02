# Spec Delta

## ADDED Requirements

### Requirement: 資料庫步驟組先於 migration 且預設只預覽
部署計畫 SHALL 新增 `database` 步驟組：建立私有網路連線所需的資源、建立資料庫執行個體、資料庫與應用程式專用的登入、設定自動備份與時間點還原、設定日誌保存期限，以及驗證憑證的連線測試（見 cloud-database 規格）。在完整計畫中，`database` SHALL 排在 `build` 之後、`migrate` 之前；已存在的資源 SHALL 被偵測並略過或更新，不重複建立。與其他步驟組一樣，預設只預覽。

#### Scenario: 順序
- **WHEN** 列出完整計畫的步驟順序
- **THEN** 資料庫步驟在映像建置之後、備份與 migration 之前

#### Scenario: 重複執行
- **WHEN** 資料庫、登入或設定已經存在
- **THEN** 對應步驟略過或以更新取代，不會建立第二份

#### Scenario: 預設只預覽
- **WHEN** 不帶執行參數執行 `database` 組
- **THEN** 只印出指令，沒有呼叫任何外部指令

### Requirement: 隔離的壓測環境是獨立的資源集
部署計畫 SHALL 支援「壓測環境」這個設定檔（profile）：它使用與預備環境相同的映像，但 SHALL 有自己的服務、資料庫、機密（含只用於假模型伺服器的測試金鑰）與假模型伺服器服務，所有資源名稱 SHALL 帶有壓測標記，與預備環境及正式環境的資源名稱沒有任何相同者；壓測服務的 `APP_ENV` 仍為 `staging`，並以 `GEMINI_BASE_URL` 指向假模型伺服器。壓測環境 SHALL NOT 讀取預備環境的機密或資料庫。

#### Scenario: 名稱不相交
- **WHEN** 以預備環境與壓測環境各預覽一次
- **THEN** 服務、資料庫、機密與排程的名稱集合不相交

#### Scenario: 假金鑰
- **WHEN** 檢視壓測服務注入的模型金鑰機密
- **THEN** 它是壓測專用的機密，不是預備環境的金鑰

#### Scenario: 指向假模型伺服器
- **WHEN** 檢視壓測服務的設定
- **THEN** `GEMINI_BASE_URL` 指向壓測環境自己的假模型伺服器網址，`APP_ENV` 為 staging

### Requirement: 服務尺寸可由呼叫者覆寫
部署計畫 SHALL 讓呼叫者覆寫 Cloud Run 服務的 CPU、記憶體、每個執行個體的並行請求數與最小及最大執行個體數，並 SHALL 把 `WEB_CONCURRENCY` 與 `GUNICORN_THREADS` 視為一般設定（納入 `.env.example` 與設定歸類，沒有未歸類的變數）。覆寫值 SHALL 被驗證為正整數與合法的單位；未覆寫時使用目前的預設值。

#### Scenario: 覆寫並行數
- **WHEN** 呼叫者指定並行數 32 與最大執行個體數 6
- **THEN** 服務部署指令含這兩個值，其餘維持預設

#### Scenario: 不合法的值
- **WHEN** 並行數為 0 或負數，或記憶體單位不合法
- **THEN** 計畫拒絕並指出參數名稱

#### Scenario: gunicorn 變數被歸類
- **WHEN** 比對 `.env.example` 與設定歸類
- **THEN** `WEB_CONCURRENCY` 與 `GUNICORN_THREADS` 各屬於一般設定

### Requirement: 單步執行以對話中的核准代替互動確認
部署腳本 SHALL 支援 `--only STEP_ID --yes`：只執行指定的**一個**步驟，並把該次執行視為已確認（供無法互動輸入的環境使用，確認由使用者在對話中針對該步驟明確給出）。`--yes` SHALL 只在同時指定 `--only` 時有效；單獨使用 `--yes`、`--only` 指定不存在的步驟或同時指定多個步驟 SHALL 被拒絕。需要使用者互動輸入的步驟（資料庫登入的密碼提示）SHALL 不能以 `--yes` 略過，必須在使用者自己的終端機執行。單步執行 SHALL 與逐步確認一樣停止於失敗，且 SHALL NOT 自動接續執行後面的步驟。

#### Scenario: 只執行一個步驟
- **WHEN** 以 `--only` 指定一個存在的步驟並加上 `--yes` 與執行參數
- **THEN** 只執行該步驟，結束後不執行任何其他步驟

#### Scenario: 單獨的 --yes
- **WHEN** 只加 `--yes` 而沒有 `--only`
- **THEN** 腳本拒絕並說明 `--yes` 必須搭配單一步驟

#### Scenario: 不存在的步驟
- **WHEN** `--only` 指定的步驟編號不在計畫中
- **THEN** 腳本拒絕並列出可用的步驟編號

#### Scenario: 互動步驟不可略過
- **WHEN** 以 `--only` 與 `--yes` 指定資料庫登入密碼提示的步驟
- **THEN** 腳本拒絕並說明必須在自己的終端機執行

### Requirement: 一次性的管理員建立工作
部署計畫 SHALL 提供 `admin` 步驟組，以一次性的 Cloud Run 工作建立首位管理員：密碼 SHALL 以暫時的機密（值從標準輸入寫入）注入工作的環境變數並以 `--password-env` 讀取，SHALL NOT 出現在指令參數或輸出；信箱與顯示名稱由呼叫者提供。工作執行後，計畫 SHALL 包含刪除該暫時機密與該工作的步驟。

#### Scenario: 密碼不經命令列
- **WHEN** 預覽 `admin` 組
- **THEN** 密碼只以機密名稱與環境變數名稱出現，沒有值

#### Scenario: 用完即刪
- **WHEN** 檢視 `admin` 組的步驟順序
- **THEN** 建立機密、執行工作之後，依序是刪除工作與刪除機密

### Requirement: 還原演練步驟組
部署計畫 SHALL 新增 `restore-drill` 步驟組，實作 cloud-restore-drill 規格的複製、工作執行、驗證與清除；它 SHALL NOT 包含在完整計畫的預設順序中（演練是週期性的獨立操作，不是每次部署的一部分），且 SHALL 需要明確的費用確認參數。

#### Scenario: 不在預設順序
- **WHEN** 預覽完整計畫
- **THEN** 不含 `restore-drill` 的任何步驟

#### Scenario: 需要費用確認
- **WHEN** 要求 `restore-drill` 組但沒有費用確認參數
- **THEN** 計畫拒絕並說明複製執行個體會產生額外費用
