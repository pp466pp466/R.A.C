# Spec Delta

## Purpose

定義展示環境的資料庫：RDS SQL Server Express 只在私有網路內被應用程式連線、連線必須驗證伺服器憑證、使用專屬的應用程式登入，並以獨立的 migration 步驟升到最新結構；初始化管理員與內建劇本，且不搬移其他環境的資料。

## ADDED Requirements

### Requirement: 資料庫不對外且只接受應用程式主機
RDS 執行個體 SHALL 不可公開存取（沒有公開位址），其安全群組 SHALL 只允許應用程式主機的安全群組連入 SQL Server 連接埠，SHALL NOT 對任何 CIDR 開放。儲存 SHALL 加密，自動備份 SHALL 啟用。

#### Scenario: 沒有公開存取
- **WHEN** 檢視 RDS 執行個體的設定
- **THEN** 公開存取為關閉，儲存加密為開啟，自動備份保留天數大於 0

#### Scenario: 只允許主機
- **WHEN** 檢視資料庫安全群組的連入規則
- **THEN** 只有一條規則，來源是應用程式主機的安全群組，沒有 CIDR 來源

### Requirement: 連線必須驗證伺服器憑證
應用程式連線 SHALL 加密並驗證 RDS 的伺服器憑證（憑證鏈與主機名稱），SHALL NOT 使用 `TrustServerCertificate=yes`，也 SHALL NOT 使用 `sa` 或 RDS 主要使用者作為應用程式登入。連線字串 SHALL NOT 使用 `ServerCertificate`（該設定是對整份憑證的精確比對、需要 strict 加密，與 RDS 不相容）。憑證信任的變更 SHALL 僅是在映像的系統憑證庫新增 RDS 的 CA（只簽發 RDS 端點憑證的信任錨）：SHALL NOT 關閉或放寬任何驗證、SHALL NOT 移除任何原有的根憑證、SHALL NOT 使用 `SSL_CERT_FILE` 之類改變整個行程信任範圍的設定，所以對其他外部服務（模型供應商、SMTP、OAuth）的憑證驗證結果不變。無法驗證時 SHALL 停止部署並回報，不得退而放寬。

#### Scenario: 驗證成功才繼續
- **WHEN** 探測步驟以應用程式的連線設定對資料庫執行最簡單的查詢
- **THEN** 連線經加密並驗證憑證後成功，才允許進行 migration 與部署

#### Scenario: 驗證失敗即停止
- **WHEN** 探測因憑證無法驗證而失敗
- **THEN** 腳本停止並回報原因，不產生任何含 `TrustServerCertificate=yes` 的設定

#### Scenario: 不影響其他連線
- **WHEN** 檢視部署用映像的系統憑證庫，與其基底映像比較
- **THEN** 憑證庫的憑證只多出 RDS 的 CA 套件的憑證、沒有減少，容器對模型供應商、SMTP 與 Google 的 HTTPS 連線仍使用系統憑證庫驗證

#### Scenario: 連線字串不放寬
- **WHEN** 檢視應用程式的資料庫連線字串（只看選項，不看密碼）
- **THEN** 含 `Encrypt=yes`，不含 `TrustServerCertificate`，也不含 `ServerCertificate`

### Requirement: 應用程式使用專屬的最小權限登入
資料庫初始化 SHALL 以 RDS 主要使用者建立一個專屬資料庫與一個專屬的應用程式登入，後者只擁有該資料庫的必要權限（含結構遷移所需）。主要使用者的密碼 SHALL 隨機產生並僅存於機密儲存，初始化完成後應用程式 SHALL 只使用專屬登入。初始化 SHALL 可重複執行（已存在即略過），且 SHALL NOT 把任何密碼寫入日誌或命令列。

#### Scenario: 重複執行
- **WHEN** 資料庫初始化執行第二次
- **THEN** 不報錯、不重設既有登入的權限以外的內容，結果與第一次相同

#### Scenario: 應用程式不用主要使用者
- **WHEN** 檢視應用程式容器取得的資料庫連線機密
- **THEN** 其登入名稱不是 RDS 主要使用者，也不是 `sa`

### Requirement: 結構遷移是先於新版程式的獨立步驟
部署 SHALL 在新版應用程式啟動之前，先建立資料庫備份（RDS 快照），再以單次容器執行 `alembic upgrade head`；SHALL NOT 在應用程式啟動時自動遷移。遷移失敗時 SHALL 停止，不啟動新版服務。展示環境的初始結構 SHALL 與主分支的最新遷移一致（目前為 `0018`）。

#### Scenario: 順序
- **WHEN** 列出部署計畫的步驟順序
- **THEN** 快照在遷移之前，遷移在啟動應用程式服務之前

#### Scenario: 遷移失敗
- **WHEN** 遷移容器以非零碼結束
- **THEN** 腳本停止，應用程式服務不被啟動或重啟

#### Scenario: 結構版本
- **WHEN** 初次部署完成後查詢目前的遷移版本
- **THEN** 版本為主分支的最新版本

### Requirement: 從空資料庫開始並初始化管理員與內建劇本
展示環境 SHALL 從空資料庫開始，SHALL NOT 含來自其他環境（Azure、本機、production）的資料，也不提供這樣的步驟。初始化 SHALL 包含執行內建劇本的種子指令，並以互動方式建立首位管理員（密碼由使用者在自己的終端機輸入，不經腳本參數與對話）。

#### Scenario: 沒有搬資料的步驟
- **WHEN** 檢視展示環境的所有步驟
- **THEN** 沒有任何一步以其他環境的資料庫或備份為來源

#### Scenario: 管理員密碼不經腳本
- **WHEN** 預覽建立管理員的步驟
- **THEN** 該步驟標為互動，密碼由使用者在連入主機的終端機輸入，不出現在任何參數或輸出
