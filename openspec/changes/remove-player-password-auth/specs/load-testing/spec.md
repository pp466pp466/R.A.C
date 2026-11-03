# Spec Delta

## MODIFIED Requirements

### Requirement: 使用者行為情境
負載測試 SHALL 模擬完整的會員行為：以預先簽出的登入 cookie 進入（玩家只用 Google 登入，沒有可呼叫的註冊或密碼登入端點）、開新對話、每 30 至 45 秒送出一則訊息，並混入讀取對話列表與以 `since_seq` 增量拉取。測試玩家 SHALL 在測試開始前於目標環境內建立（已完成年齡確認的成年玩家，條款已同意），並以該環境自己的 `SECRET_KEY` 簽出與真實登入相同的 session cookie 交給測試程式；應用程式映像 SHALL NOT 為此新增任何能簽發登入狀態的指令或端點。虛擬使用者數、持續時間與加壓方式 SHALL 可由參數指定，預設為 100 位虛擬使用者、持續 15 分鐘。每位虛擬使用者 SHALL 使用自己的帳號，且 SHALL 通過 CSRF 檢查。

#### Scenario: 一位虛擬使用者的流程
- **WHEN** 一位虛擬使用者開始
- **THEN** 帶著預先簽出的 cookie 開新對話，之後週期性送出訊息並收到回覆

#### Scenario: 參數
- **WHEN** 指定 20 位虛擬使用者與 1 分鐘
- **THEN** 測試依此規模執行

#### Scenario: cookie 數量不足
- **WHEN** 指定的虛擬使用者數多於預先簽出的 cookie 數
- **THEN** 測試在送出任何請求之前失敗並說明缺少的數量

#### Scenario: 映像沒有簽發登入狀態的途徑
- **WHEN** 檢查容器映像的內容與應用程式的路由
- **THEN** 沒有任何指令或端點能在不經 Google 或管理員密碼的情況下建立登入 session

### Requirement: 只對測試或預備環境執行
負載測試 SHALL 預設只對本機位址執行；對其他位址執行 SHALL 需要明確的參數確認，且 SHALL 拒絕對文件所列的正式環境網域執行。簽出測試 cookie 的步驟 SHALL 拒絕在 `production` 執行。測試產生的帳號 SHALL 使用保留的測試網域信箱，SHALL NOT 寄出任何真實信件。

#### Scenario: 預設本機
- **WHEN** 不指定目標位址
- **THEN** 對本機位址執行

#### Scenario: 其他位址需確認
- **WHEN** 指定非本機位址而沒有確認參數
- **THEN** 測試拒絕執行

#### Scenario: 正式網域
- **WHEN** 指定的位址是正式環境網域
- **THEN** 即使有確認參數也拒絕執行

#### Scenario: 正式環境不簽出 cookie
- **WHEN** 在 `APP_ENV` 為 `production` 的環境執行簽出測試 cookie 的步驟
- **THEN** 拒絕執行並以非零代碼結束，沒有建立任何帳號

#### Scenario: 測試帳號
- **WHEN** 建立測試玩家
- **THEN** 信箱屬於保留的測試網域
