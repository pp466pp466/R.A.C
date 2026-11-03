# Spec Delta

## RENAMED Requirements

- FROM: `### Requirement: 登入失敗激增告警`
- TO: `### Requirement: 帳號鎖定激增告警`

## MODIFIED Requirements

### Requirement: 帳號鎖定激增告警
系統 SHALL 以目前處於鎖定的帳號數評估：達 5 個為警告。計入的鎖定有兩種：管理員密碼連續失敗造成的鎖定（鎖定期間尚未結束；15 分鐘後自動解除）與玩家帳號的自動鎖定（`account-lockout`，管理員解除前一直計入）。同一個帳號被反覆嘗試或同時符合兩種只算一個。告警的內部代碼維持不變（狀態資料表不需要遷移），顯示名稱為「帳號鎖定激增」。

#### Scenario: 五個帳號被鎖定
- **WHEN** 目前有 5 個帳號處於鎖定
- **THEN** 該告警為警告

#### Scenario: 兩種鎖定合計
- **WHEN** 2 個管理員密碼鎖定中與 3 個玩家帳號被鎖定
- **THEN** 該告警為警告

#### Scenario: 一個帳號被反覆嘗試
- **WHEN** 同一個帳號被鎖定
- **THEN** 該告警為正常

#### Scenario: 鎖定已到期
- **WHEN** 原本因密碼失敗被鎖定的管理員鎖定期間已結束
- **THEN** 不計入

#### Scenario: 管理員解除後
- **WHEN** 管理員解除一個玩家帳號的鎖定
- **THEN** 不再計入
