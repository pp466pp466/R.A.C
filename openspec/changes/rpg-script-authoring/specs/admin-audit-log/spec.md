# Spec Delta

## MODIFIED Requirements

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
