# Spec Delta

## ADDED Requirements

### Requirement: 鎖定計次紀錄保存 7 天
鎖定計次紀錄（見 `account-lockout`）SHALL 在建立 7 天後被清除（視窗只有 15 分鐘，更久的紀錄沒有用途）。清除 SHALL 由既有的期限清除指令負責，預覽模式只列筆數，輸出只有各類別的筆數。申訴不納入期限清除，隨所屬帳號保存與刪除。

#### Scenario: 超過 7 天
- **WHEN** 一筆計次紀錄建立在 8 天前，執行期限清除
- **THEN** 該筆被刪除

#### Scenario: 7 天內
- **WHEN** 一筆計次紀錄建立在 6 天前
- **THEN** 保留

#### Scenario: 申訴不被清除
- **WHEN** 一則申訴建立在 400 天前且帳號仍存在
- **THEN** 不被期限清除刪除
