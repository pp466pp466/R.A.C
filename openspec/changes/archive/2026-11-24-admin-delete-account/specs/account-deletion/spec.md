## ADDED Requirements

### Requirement: 管理員刪除帳號走同一個抹除出口
管理員在後台刪除帳號（見 `admin-user-management`）SHALL 立即抹除，不經 `deletion_pending` 與 7 天寬限期，並 SHALL 走與其他抹除路徑相同的單一抹除出口：抹除的範圍與「抹除的範圍」需求完全相同（唯一差別是不保留劇本，見 `script-anonymous-retention`），且 SHALL 在同一個交易內寫入一筆只含內部使用者編號、時間與原因 `admin_deleted` 的刪除紀錄，不含 email、名稱、生日或任何個資。刪除條件 SHALL 在刪除當下再確認一次：目標若已是管理員，SHALL NOT 被刪除，整個交易 SHALL 回復（含該次的稽核紀錄），且不留下刪除紀錄。備份還原後依刪除紀錄重新抹除時 SHALL 以編號重抹、不區分原因。

#### Scenario: 刪除紀錄的原因
- **WHEN** 管理員刪除一位玩家
- **THEN** 新增一筆原因為 `admin_deleted` 的刪除紀錄，只有內部編號、時間與原因

#### Scenario: 抹除的範圍相同
- **WHEN** 管理員刪除一位有對話、存檔、記事簿、遊戲幣、劇本、檢舉、申訴與第三方身分的玩家
- **THEN** 這些資料都被移除，用量帳本保留但使用者編號與對話編號為空值，其他玩家用該作者劇本開出的存檔保留且對來源版本的參照為空值，管理稽核紀錄保留

#### Scenario: 條件不成立時整個回復
- **WHEN** 目標在檢查之後、刪除之前變成管理員
- **THEN** 該帳號沒有被刪除、沒有刪除紀錄、也沒有 `user.delete` 稽核，管理員得到 409 `state_changed`

#### Scenario: 還原後重新抹除
- **WHEN** 備份還原後依刪除紀錄重新抹除一個原因為 `admin_deleted` 的編號
- **THEN** 該編號對應的帳號被抹除，與其他原因的處理相同

#### Scenario: 管理員不可自助刪除的規則不變
- **WHEN** 管理員申請以自助流程刪除自己的帳號
- **THEN** 仍得到 403 `admin_delete_forbidden`
