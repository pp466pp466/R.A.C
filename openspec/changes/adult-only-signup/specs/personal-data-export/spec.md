# Spec Delta

## ADDED Requirements

### Requirement: 匯出納入年滿 18 歲聲明
帳號資料 SHALL 另含註冊時年滿 18 歲聲明的時間與當時的年齡政策版本。沒有聲明紀錄的帳號（管理員帳號與升級前的帳號）SHALL 以空值匯出，且 SHALL NOT 因此出錯。

#### Scenario: 含聲明紀錄
- **WHEN** 註冊時聲明過年滿 18 歲的使用者匯出
- **THEN** 帳號資料包含聲明的時間與政策版本

#### Scenario: 沒有聲明紀錄
- **WHEN** 沒有聲明紀錄的管理員匯出
- **THEN** 這兩個欄位為空值，匯出照常完成
