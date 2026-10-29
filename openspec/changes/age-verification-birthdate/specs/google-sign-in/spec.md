# Spec Delta

## RENAMED Requirements

- FROM: `### Requirement: 首次以 Google 註冊須確認年滿 18 歲`
- TO: `### Requirement: 首次以 Google 註冊須通過年齡驗證`

## MODIFIED Requirements

### Requirement: 首次以 Google 註冊須通過年齡驗證
需要建立新帳號時，系統 SHALL 要求發起流程時已同意條款（獨立的勾選），並已提供通過驗證的生日與年齡區間（規則見 `age-confirmation`）。驗證 SHALL 在發起時（`/api/auth/google/start`）就完成：未滿 13 歲或兩者不一致時 SHALL 立即回 400，使用者不會被導向 Google，也不保存任何生日。通過驗證的生日與區間 SHALL 只放在已簽章、10 分鐘有效的交易 cookie，回呼成功建立新帳號時才寫入資料庫，回呼結束（成功或失敗）SHALL 一律刪除該 cookie。沒有生日與區間的交易（例如更新前發起的流程）SHALL 視為未提供，不建立帳號並導向登入頁提示須先完成年齡確認。已存在的帳號（含已綁定 Google 或以信箱合併的帳號）登入時不需要這些資料，也 SHALL NOT 因此被標記為已完成年齡確認，但在補填之前同樣不能使用 AI 功能。

#### Scenario: 新使用者未確認年滿 18 歲
- **WHEN** 沒有帳號的使用者沒有生日與區間就以 Google 登入
- **THEN** 不建立帳號，導向登入頁並提示須先完成年齡確認

#### Scenario: 發起時就被擋下
- **WHEN** 使用者在發起 Google 登入時提交未滿 13 歲的生日，或與區間不一致的生日
- **THEN** 回應 400，不導向 Google，沒有任何資料被保存

#### Scenario: 新使用者已確認
- **WHEN** 沒有帳號的使用者同意條款並提供一致的生日與區間後以 Google 登入
- **THEN** 建立帳號並記錄生日、年齡區間、確認時間與政策版本

#### Scenario: 同意條款與確認年齡各自獨立
- **WHEN** 沒有帳號的使用者只完成其中一項就以 Google 登入
- **THEN** 不建立帳號，並導向登入頁提示須先完成尚未完成的那一項

#### Scenario: 既有的未確認帳號
- **WHEN** 尚未完成年齡確認的既有帳號以 Google 登入
- **THEN** 登入成功且仍標示為尚未完成，在補填之前仍不能使用 AI 功能

#### Scenario: 交易內容的生命週期
- **WHEN** Google 回呼結束（成功或失敗）
- **THEN** 含生日的交易 cookie 已被刪除，同一個回呼網址無法再次使用
