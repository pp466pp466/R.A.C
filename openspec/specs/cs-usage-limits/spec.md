# cs-usage-limits Specification

## Purpose
定義每位使用者在客服上的用量限制：依會員等級的每分鐘訊息數、每日對話輪數與對話數上限，超額時如何拒絕、如何說明，以及使用者如何查看自己的用量。限制只依等級，不依角色。

## Requirements

### Requirement: 各會員等級的限制
系統 SHALL 依使用者的會員等級套用下列限制，且限制的數值 SHALL 只有一個來源：每日對話輪數 free 30、plus 100、vip 300；每分鐘訊息數 free 6、plus 10、vip 20；對話數上限 free 20、plus 100、vip 500；單則訊息上限一律 1000 字。等級不在上述範圍時 SHALL 套用 free 的限制。管理員 SHALL NOT 因角色而豁免。

#### Scenario: 各等級取得各自的上限
- **WHEN** 查詢 free、plus、vip 使用者的限制
- **THEN** 依序得到 30 / 6 / 20、100 / 10 / 100、300 / 20 / 500（每日輪數／每分鐘／對話數）

#### Scenario: 不認得的等級
- **WHEN** 使用者的等級值不在 free、plus、vip 之中
- **THEN** 套用 free 的限制

#### Scenario: 管理員不豁免
- **WHEN** 角色為 admin 的 free 會員送出第 31 則當日訊息
- **THEN** 與一般 free 會員一樣被拒絕

### Requirement: 客服所述的額度與實際限制一致
客服 system prompt 中說明的各等級額度 SHALL 由同一份限制產生，而不是另外手寫的數字。限制改變時，客服對使用者說明的額度 SHALL 隨之改變。

#### Scenario: 提示詞與限制一致
- **WHEN** 檢查客服 system prompt 中的額度說明
- **THEN** 其中每個等級的每日輪數、每分鐘訊息數與對話數上限都等於限制表的值

### Requirement: 每分鐘訊息數限制
送出訊息時，系統 SHALL 計算該使用者在最近 60 秒內已被接受的訊息數；達到其等級的每分鐘上限時 SHALL 回應 429 與代碼 `rate_limited`，並以 `Retry-After` 標頭給出最早可再送出的秒數。被拒絕的訊息 SHALL NOT 被儲存，也 SHALL NOT 計入之後的計數。

#### Scenario: 一分鐘內超過上限
- **WHEN** free 會員在 60 秒內已送出 6 則被接受的訊息，又送出第 7 則
- **THEN** 回應 429、代碼 `rate_limited`、繁體中文說明與 `Retry-After`；第 7 則沒有被儲存，也沒有呼叫模型

#### Scenario: 視窗滑動後恢復
- **WHEN** 最早的那則訊息已超過 60 秒
- **THEN** 使用者可以再送出訊息

#### Scenario: 被拒絕的訊息不影響計數
- **WHEN** 使用者連續被拒絕三次，等到視窗內較舊的訊息過期後重試
- **THEN** 計數只反映被接受的訊息，使用者可以送出

### Requirement: 每日對話輪數限制
送出訊息時，系統 SHALL 以 Asia/Taipei 的日界計算該使用者今日已得到正常 AI 回覆的輪數，加上目前正在處理中的其他對話數；達到其等級的每日上限時 SHALL 回應 429 與代碼 `daily_quota_exceeded`。降級回覆（fallback）與摘要呼叫 SHALL NOT 計入。

#### Scenario: 用完今日額度
- **WHEN** free 會員今日已有 30 則正常回覆，再送出一則
- **THEN** 回應 429、代碼 `daily_quota_exceeded`，說明額度已用完並於明日 00:00（台灣時間）重置；沒有儲存訊息也沒有呼叫模型

#### Scenario: 降級回覆不計入
- **WHEN** 今日有 5 則訊息只得到降級文案，其餘正常回覆共 29 則
- **THEN** 使用者仍可再送出一則

#### Scenario: 日界以台灣時間計算
- **WHEN** 現在是台灣時間 00:00 剛過
- **THEN** 前一個台灣日的回覆不再計入今日

#### Scenario: 同時處理中的訊息先保留名額
- **WHEN** free 會員今日已有 29 則回覆，另一個對話有一則訊息正在處理中，這時又在別的對話送出
- **THEN** 回應 429、代碼 `daily_quota_exceeded`

### Requirement: 對話數上限
建立對話時，系統 SHALL 在新對話會超過其等級的對話數上限時回應 409 與代碼 `conversation_limit_reached`，並說明可先刪除不需要的對話。以同一個前端識別碼重送已建立的對話 SHALL 照常回傳該對話，不受上限影響。刪除對話後 SHALL 立即釋出名額。

#### Scenario: 達到上限
- **WHEN** free 會員已有 20 個對話，再以新的識別碼建立
- **THEN** 回應 409、代碼 `conversation_limit_reached`，沒有建立新對話

#### Scenario: 冪等重送不被擋
- **WHEN** 已有 20 個對話的 free 會員以其中一個對話的識別碼重送建立
- **THEN** 回應 200 與該對話

#### Scenario: 刪除後可再建立
- **WHEN** 已有 20 個對話的 free 會員刪除一個後建立新對話
- **THEN** 回應 201

### Requirement: 重送已處理的訊息不受限制
以同一個訊息識別碼重送、且該訊息已有回覆時，系統 SHALL 直接回傳既有回覆，SHALL NOT 套用任何用量限制，也 SHALL NOT 使其被重複計入。已儲存但沒有回覆的訊息（上次中斷）被重送時，SHALL 套用每日輪數與全站預算與暫停檢查，但 SHALL NOT 再套用每分鐘限制，因為它已被接受過一次。

#### Scenario: 額度用完後重送舊訊息
- **WHEN** 今日額度已用完的使用者重送一則先前已得到回覆的訊息
- **THEN** 回應 200 與既有回覆

#### Scenario: 中斷的訊息重送
- **WHEN** 使用者重送一則已儲存但沒有回覆的訊息，且最近一分鐘內已送滿上限
- **THEN** 不因每分鐘限制被拒絕，繼續處理

### Requirement: 檢查順序與拒絕時不留痕
送出訊息時，系統 SHALL 依序檢查：帳號與年齡閘門、已處理訊息的冪等回傳、對話暫停、每分鐘限制、每日輪數、全站預算，之後才取得該對話的處理標記。任何一項拒絕時，系統 SHALL NOT 儲存訊息、SHALL NOT 設定處理標記、SHALL NOT 呼叫模型、SHALL NOT 新增帳本紀錄。

#### Scenario: 同時違反多項
- **WHEN** 被暫停的使用者同時超過每分鐘限制
- **THEN** 回應的是暫停（`chat_suspended`），不是 `rate_limited`

#### Scenario: 拒絕後資料不變
- **WHEN** 任一項限制拒絕了一則訊息
- **THEN** 訊息數、帳本筆數與該對話的處理標記都與拒絕前相同

### Requirement: 超額文案使用繁體中文
所有因用量而拒絕的回應 SHALL 使用繁體中文，且不含內部細節。每分鐘限制：「您傳送訊息的速度太快了，請稍候幾秒再試。」每日額度：「您今日的對話額度已用完，將於明日 00:00（台灣時間）重置。」對話數：「對話數已達上限，請先刪除不需要的對話。」

#### Scenario: 文案
- **WHEN** 任一限制拒絕請求
- **THEN** 回應的 `error.message` 為上列對應的繁體中文文案

### Requirement: 查看自己的用量
`GET /api/cs/usage` SHALL 要求登入，只回傳呼叫者自己的用量：會員等級、今日已用與上限的輪數與重置時間、每分鐘上限、對話數與上限、對話暫停的截止時間（沒有暫停為空）。回應 SHALL NOT 含全站用量、token 數或其他使用者的資料。

#### Scenario: 查詢用量
- **WHEN** free 會員今日已有 3 則正常回覆、2 個對話後呼叫
- **THEN** 回應包含等級 free、今日已用 3 且上限 30、每分鐘上限 6、對話數 2 且上限 20、暫停為空

#### Scenario: 未登入
- **WHEN** 未登入者呼叫
- **THEN** 回應 401
