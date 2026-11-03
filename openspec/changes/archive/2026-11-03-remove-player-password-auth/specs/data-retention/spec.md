# Spec Delta

## MODIFIED Requirements

### Requirement: 驗證權杖在到期後 7 天清除
重設密碼的權杖（含遷移前遺留的信箱驗證權杖），到期時間早於 7 天前者 SHALL 被刪除，不論是否已使用。

#### Scenario: 到期超過 7 天
- **WHEN** 一個權杖已到期超過 7 天
- **THEN** 被刪除

#### Scenario: 遺留的信箱驗證權杖
- **WHEN** 遷移前發放的信箱驗證權杖已到期超過 7 天
- **THEN** 被刪除，不需要另外的清除途徑

#### Scenario: 剛到期
- **WHEN** 一個權杖剛到期
- **THEN** 保留
