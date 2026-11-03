# Spec Delta

## MODIFIED Requirements

### Requirement: 動作是固定的列舉
稽核的動作 SHALL 只有：`user.lookup`、`user.suspend`、`user.unsuspend`、`user.change_tier`、`conversation.read`、`conversation.delete`、`failures.read`、`reauth.failed`，以及帳號鎖定相關的 `account.lock`、`appeal.submit`、`appeal.read`、`account.unlock`。`account.lock` 與 `appeal.submit` 由系統或玩家觸發，其管理員欄位 SHALL 為空；其餘由管理員觸發。寫入列舉以外的動作 SHALL 被拒絕。申訴內容 SHALL NOT 進入稽核紀錄。

#### Scenario: 未知的動作
- **WHEN** 程式嘗試寫入動作 `user.delete_everything`
- **THEN** 寫入被拒絕，沒有任何紀錄產生

#### Scenario: 系統觸發的鎖定
- **WHEN** 帳號因門檻被自動鎖定
- **THEN** 有一筆 `account.lock` 紀錄，管理員欄位為空，目標是該帳號的對外識別碼

#### Scenario: 申訴不進稽核內容
- **WHEN** 玩家提交申訴
- **THEN** `appeal.submit` 紀錄只含目標識別碼與字數，不含申訴文字
