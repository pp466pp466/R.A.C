# admin-audit-log Specification

## Purpose
定義管理員操作的稽核紀錄：哪些動作一定留下紀錄、紀錄裡有什麼與沒有什麼、它如何保證與動作一起成立、不可被修改，以及管理員如何查看。後台是高權限的入口，這份紀錄是「誰在何時對誰做了什麼」的唯一依據。

## Requirements




### Requirement: 稽核紀錄的內容
每筆稽核紀錄 SHALL 含：執行的管理員（管理員帳號被刪除時為空值，紀錄保留）、固定列舉的動作、目標使用者的對外識別碼（可為空）、目標對話的對外識別碼（可為空）、管理員填寫的原因（可為空，至多 200 字）、列舉型的細節（例如調整前後的等級、原狀態、筆數、查詢的途徑為信箱或識別碼），以及建立時間（UTC）。目標只存對外識別碼，SHALL NOT 以外鍵參照使用者，使用者被刪除後紀錄仍然成立。

#### Scenario: 調整等級的紀錄
- **WHEN** 管理員把使用者從 free 調整為 plus
- **THEN** 紀錄含管理員、動作 `user.change_tier`、目標使用者識別碼，細節為 free 與 plus

#### Scenario: 目標被刪除之後
- **WHEN** 被操作過的使用者之後被刪除
- **THEN** 稽核紀錄仍在，目標識別碼保留

### Requirement: 動作是固定的列舉
稽核的動作 SHALL 只有：`user.lookup`、`user.suspend`、`user.unsuspend`、`user.change_tier`、`conversation.read`、`conversation.delete`、`failures.read`、`reauth.failed`，帳號鎖定相關的 `account.lock`、`appeal.submit`、`appeal.read`、`account.unlock`，以及劇本創作相關的 `limit.set`（部署者修改限制）、`publish.suspend`（連續被自動拒絕而暫停發佈）、`publish.lift`（管理員解除暫停）。`account.lock`、`appeal.submit`、`limit.set` 與 `publish.suspend` 由系統、玩家或部署者觸發，其管理員欄位 SHALL 為空；其餘由管理員觸發。寫入列舉以外的動作 SHALL 被拒絕。申訴內容 SHALL NOT 進入稽核紀錄；`limit.set` 的細節 SHALL 只含限制的鍵與新舊數值。

#### Scenario: 未知的動作
- **WHEN** 程式嘗試寫入動作 `user.delete_everything`
- **THEN** 寫入被拒絕，沒有任何紀錄產生

#### Scenario: 系統觸發的鎖定
- **WHEN** 帳號因門檻被自動鎖定
- **THEN** 有一筆 `account.lock` 紀錄，管理員欄位為空，目標是該帳號的對外識別碼

#### Scenario: 申訴不進稽核內容
- **WHEN** 玩家提交申訴
- **THEN** `appeal.submit` 紀錄只含目標識別碼與字數，不含申訴文字

#### Scenario: 系統觸發的暫停發佈
- **WHEN** 作者因連續被自動拒絕而被暫停發佈
- **THEN** 有一筆 `publish.suspend` 紀錄，管理員欄位為空，目標是該帳號的對外識別碼

#### Scenario: 修改限制
- **WHEN** 部署者修改一個限制
- **THEN** 有一筆 `limit.set` 紀錄，管理員欄位為空，細節只有鍵與新舊數值

### Requirement: 稽核不記錄敏感內容
稽核紀錄 SHALL NOT 含 email、顯示名稱、訊息或回覆文字、模型原文、密碼與其雜湊、權杖。管理員填寫的原因是唯一的自由文字，且只在停權與刪除對話時存在。以信箱查詢使用者時，紀錄 SHALL 只含查到的使用者的對外識別碼，不含被查詢的信箱。

#### Scenario: 以信箱查詢
- **WHEN** 管理員以 `someone@example.com` 查詢使用者
- **THEN** 稽核紀錄的任何欄位都不含這個信箱

#### Scenario: 讀取對話
- **WHEN** 管理員讀取一個對話
- **THEN** 稽核紀錄只有對話與使用者的對外識別碼，沒有任何訊息文字

### Requirement: 稽核與動作同時成立
改變資料的操作與它的稽核紀錄 SHALL 在同一個資料庫交易：兩者一起成功或一起不發生。讀取類的操作 SHALL 在回傳內容之前寫入稽核，寫入失敗時 SHALL NOT 回傳內容。重新驗證失敗（`reauth.failed`）SHALL 即使回應是錯誤也被保存。

#### Scenario: 動作失敗
- **WHEN** 停權在寫入階段發生例外
- **THEN** 使用者狀態與稽核都沒有改變

#### Scenario: 稽核寫入失敗
- **WHEN** 讀取對話時稽核無法寫入
- **THEN** 回應錯誤，沒有回傳任何訊息內容

#### Scenario: 密碼錯誤
- **WHEN** 管理員以錯誤的密碼嘗試刪除對話
- **THEN** 回應 400，且 `reauth.failed` 稽核仍被保存

### Requirement: 稽核紀錄不可被修改
系統 SHALL NOT 提供任何 API、Service 方法或 Repository 方法來修改或刪除稽核紀錄（保存期限屆滿後的清除由另外的排程指令負責）。

#### Scenario: 沒有修改的路徑
- **WHEN** 檢查稽核的 Repository 的公開方法
- **THEN** 只有新增與讀取，沒有更新或刪除

#### Scenario: API
- **WHEN** 管理員以 PUT、PATCH 或 DELETE 呼叫稽核端點
- **THEN** 端點不存在（404 或 405）

### Requirement: 管理員可查看稽核紀錄
`GET /api/admin/audit` SHALL 回傳稽核紀錄，由新到舊，每頁預設 50 筆、最多 100 筆，每筆含時間、動作、執行的管理員的顯示名稱（已刪除為空）、目標識別碼、原因與細節。回應 SHALL NOT 含管理員的 email。查看稽核本身 SHALL NOT 再寫入稽核。

#### Scenario: 查看
- **WHEN** 管理員先停權一位使用者，再查看稽核
- **THEN** 最新一筆是 `user.suspend`，含原因與目標識別碼

#### Scenario: 一般使用者
- **WHEN** 一般使用者呼叫稽核端點
- **THEN** 回應 404
