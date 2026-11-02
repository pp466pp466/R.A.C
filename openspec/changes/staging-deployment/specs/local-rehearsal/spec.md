# Spec Delta

## Purpose

定義在沒有雲端專案時，如何以本機單人預演驗證預備環境的行為：以 staging 設定在本機啟動、經 HTTPS 通道對外、跑冒煙驗收與 Google 登入、讓營運告警信真實寄出，並誠實標明預演驗證了什麼、哪些必須等雲端執行才算數，避免把本機的結果當成雲端的結果。

## ADDED Requirements

### Requirement: 以 staging 設定在本機預演
系統 SHALL 提供在本機預演預備環境的步驟：`APP_ENV` 為 `staging`、資料庫為 SQLite 檔案（不含任何正式資料，檔案不進版本庫）、機密與一般設定來自本機被忽略的檔案、經 HTTPS 通道取得 https 網址並將 `BASE_URL` 設為該網址。啟動 SHALL 通過與雲端相同的設定驗證（缺少必要機密即啟動失敗、不得使用 `sa` 與 `TrustServerCertificate=yes`、`staging` 信任入口層標頭）；預演 SHALL NOT 放寬任何 staging 的檢查。

#### Scenario: 設定驗證仍生效
- **WHEN** 以 staging 設定在本機啟動但缺少 `SECRET_KEY`
- **THEN** 啟動失敗，訊息只指出變數名稱

#### Scenario: 經通道的 https 網址
- **WHEN** 以 staging 設定啟動並經 HTTPS 通道存取
- **THEN** 回應帶 HSTS，session 與 CSRF cookie 使用 `__Host-` 前綴並帶 `Secure`

#### Scenario: 資料庫檔案不進版本庫
- **WHEN** 預演產生 SQLite 資料庫檔案
- **THEN** 該檔案被版本控制忽略

### Requirement: 預演通過冒煙驗收
本機預演 SHALL 對通道網址執行冒煙驗收腳本並全部通過（模型呼叫除外，預設略過）；失敗的項目 SHALL 在繼續之前修正，並在驗收紀錄中記錄原因。

#### Scenario: 通過
- **WHEN** 對預演的通道網址執行冒煙腳本
- **THEN** 所有檢查項目通過，結束代碼為 0

### Requirement: 營運告警信以真實寄送驗證
預演 SHALL 讓 `check-alerts` 寄出一封真實的告警信：在預演的資料庫製造一項告警條件（例如 5 個被鎖定的測試帳號，信箱使用保留的 `.invalid` 網域），執行指令後由使用者確認信件送達；紀錄 SHALL 包含是否進垃圾郵件匣以及寄件者的送達風險觀察。沒有收到信時 SHALL 標為「未驗證」並記錄推測的原因，SHALL NOT 以預期結果代替。驗證後 SHALL 清除製造的測試資料。

#### Scenario: 收到信
- **WHEN** 製造告警條件並執行 `check-alerts`
- **THEN** 使用者的信箱收到一封「進入告警」的信，內容只有告警名稱、數值與門檻，沒有任何使用者資料

#### Scenario: 沒收到
- **WHEN** 預期窗口內沒有收到信
- **THEN** 紀錄標為未驗證，並寫明檢查過的地方（垃圾郵件匣、寄信服務的後台紀錄）與推測原因

#### Scenario: 測試資料清除
- **WHEN** 驗證完成
- **THEN** 製造的測試帳號與告警狀態已清除

### Requirement: 本機容器預演為條件式
本機容器環境可用時，預演 SHALL 以主映像（非 root、gunicorn gthread）搭配假模型伺服器（透過 `GEMINI_BASE_URL`）與階梯式加壓進行；環境不可用時 SHALL 標為「未預演」並說明原因，SHALL NOT 以 Flask 開發伺服器的數字代替。本機容器的結果 SHALL 標明不是容量依據。

#### Scenario: 容器可用
- **WHEN** 本機容器環境可用
- **THEN** 以主映像啟動，假模型伺服器回應，階梯式加壓完成並留下記錄

#### Scenario: 容器不可用
- **WHEN** 本機容器環境無法啟動
- **THEN** 紀錄標為未預演並說明原因，沒有以其他伺服器的數字代替

### Requirement: 預演範圍的誠實揭露
驗收紀錄 SHALL 分兩欄列出「本機預演已驗證」與「必須在雲端驗證（尚未做）」。後者至少包含：託管資料庫的私有連線與憑證驗證、Cloud Run 服務與 Secret Manager 的注入、Cloud Scheduler 與 Cloud Run 工作、平台告警與演練、雲端的壓測容量、雲端的還原演練。這些項目在雲端執行完成之前 SHALL 維持未驗證，上線前關卡 SHALL NOT 因本機預演而標為已通過。

#### Scenario: 兩欄並列
- **WHEN** 檢視預演的驗收紀錄
- **THEN** 已驗證與必須在雲端驗證的項目分開列出，後者標為尚未做

#### Scenario: 關卡不因預演通過
- **WHEN** 本機預演全部通過
- **THEN** 「負載測試」與「託管資料庫成本」關卡的狀態不變為已通過
