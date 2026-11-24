## MODIFIED Requirements

### Requirement: 動作是固定的列舉
稽核的動作 SHALL 只有：`user.lookup`、`user.suspend`、`user.unsuspend`、`user.change_tier`、`user.delete`（管理員刪除帳號）、`conversation.read`、`conversation.delete`、`failures.read`、`reauth.failed`，帳號鎖定相關的 `account.lock`、`appeal.submit`、`appeal.read`、`account.unlock`，劇本創作相關的 `limit.set`（部署者修改限制）、`publish.suspend`（連續被自動拒絕而暫停發佈）、`publish.lift`（管理員解除暫停），遊戲幣相關的 `wallet.adjust`（管理員調整遊戲幣餘額），劇本審核相關的 `script.read`（管理員讀取送審過的劇本全文）、`script.approve`（核可）、`script.reject`（人工退回）、`script.remove`（管理員下架）、`script.restore`（管理員恢復上架），以及劇本檢舉相關的 `report.read`（管理員讀取版本的檢舉）、`report.dismiss`（管理員結案為不成立）。`account.lock`、`appeal.submit`、`limit.set` 與 `publish.suspend` 由系統、玩家或部署者觸發，其管理員欄位 SHALL 為空；其餘由管理員觸發。寫入列舉以外的動作 SHALL 被拒絕。申訴內容 SHALL NOT 進入稽核紀錄；`limit.set` 的細節 SHALL 只含限制的鍵與新舊數值；`wallet.adjust` 的細節 SHALL 只含調整的餘額種類、增減量與調整後的餘額；`user.delete` 的目標 SHALL 是被刪帳號的對外識別碼，細節 SHALL 只含原狀態的列舉值，SHALL NOT 含 email、暱稱、顯示名稱與任何使用者文字，管理員填寫的原因 SHALL 存在原因欄位；劇本審核動作的細節 SHALL 只含版本與劇本的對外識別碼、狀態與類別列舉，SHALL NOT 含劇本文字、標題、筆名與審核備註；檢舉動作的細節 SHALL 只含版本與劇本的對外識別碼與整數筆數，SHALL NOT 含檢舉的自寫文字與理由、檢舉人的任何資訊、劇本文字、標題與筆名。

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

#### Scenario: 調整遊戲幣
- **WHEN** 管理員調整一位玩家的遊戲幣
- **THEN** 有一筆 `wallet.adjust` 紀錄，管理員欄位是該管理員，目標是玩家的對外識別碼，細節只有餘額種類、增減量與調整後的餘額

#### Scenario: 刪除帳號
- **WHEN** 管理員刪除一位玩家的帳號
- **THEN** 有一筆 `user.delete` 紀錄，管理員欄位是該管理員，目標是被刪帳號的對外識別碼，原因欄位是管理員填的原因，細節只有原狀態，找不到 email 或暱稱

#### Scenario: 刪除之後稽核仍在
- **WHEN** 被刪帳號的紀錄在刪除之後被檢視
- **THEN** `user.delete` 與該帳號先前的稽核紀錄都還在，目標只顯示對外識別碼

#### Scenario: 核可劇本
- **WHEN** 管理員核可一份待審劇本
- **THEN** 有一筆 `script.approve` 紀錄，管理員欄位是該管理員，細節有版本與劇本的對外識別碼，沒有標題與任何劇本文字

#### Scenario: 退回不記備註
- **WHEN** 管理員以審核備註退回劇本
- **THEN** `script.reject` 紀錄的細節只有類別與識別碼，找不到備註的文字

#### Scenario: 讀取劇本
- **WHEN** 管理員讀取送審過的劇本全文
- **THEN** 回傳之前已有一筆 `script.read` 紀錄

#### Scenario: 讀取檢舉
- **WHEN** 管理員讀取一個版本的檢舉
- **THEN** 回傳之前已有一筆 `report.read` 紀錄，細節只有版本與劇本的對外識別碼與筆數，沒有檢舉的文字

#### Scenario: 結案不成立
- **WHEN** 管理員把一個版本的檢舉結為不成立
- **THEN** 有一筆 `report.dismiss` 紀錄，管理員欄位是該管理員，細節只有識別碼與結案筆數

#### Scenario: 已註銷作者的劇本
- **WHEN** 管理員對作者已註銷的劇本讀取檢舉
- **THEN** 稽核紀錄的目標使用者為空，細節仍有版本與劇本的識別碼
