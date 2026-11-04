# Spec Delta

## MODIFIED Requirements

### Requirement: 誰可以用哪個版本開存檔
開存檔 SHALL 先通過帳號與年齡閘門（帳號啟用、已完成年齡確認、年齡不低於最低使用年齡，管理員與各會員等級不豁免）。任何通過的玩家 SHALL 只能用 `published` 的版本開存檔；擁有者 SHALL 另外可以用自己劇本的任何版本開存檔。非擁有者用他人的非上架版本開存檔 SHALL 得到 404。劇本宣告的 `content_tier` 高於玩家目前生效的內容模式時 SHALL 被拒絕（`403`，代碼 `content_tier_restricted`），不開出存檔。非內建劇本的版本 SHALL 另外通過模型層審查的把關（對應目前內容的結果為 `pass` 或 `flag`，見 `script-review`），未通過時不開出存檔。

#### Scenario: 用已發佈的版本
- **WHEN** 已完成年齡確認的成年玩家用內建劇本開存檔
- **THEN** 開出存檔，狀態為進行中

#### Scenario: 未完成年齡確認
- **WHEN** 尚未完成年齡確認的玩家嘗試開存檔
- **THEN** 回應 403 與代碼 `age_confirmation_required`，沒有開出存檔

#### Scenario: 擁有者用自己的草稿
- **WHEN** 作者用自己的 `draft` 版本開存檔，且該內容的模型層審查為 `pass`
- **THEN** 開出存檔

#### Scenario: 非擁有者用他人的草稿
- **WHEN** 另一位玩家嘗試用別人的 `draft` 版本開存檔
- **THEN** 得到與不存在相同的 404

#### Scenario: 劇本分級高於玩家模式
- **WHEN** 安全模式的玩家嘗試用宣告為 `standard` 的劇本開存檔
- **THEN** 回應 403 與代碼 `content_tier_restricted`，沒有開出存檔

#### Scenario: 審查未通過
- **WHEN** 作者用審查結果為 `reject` 的草稿開存檔
- **THEN** 回應 422 與代碼 `script_rejected`，沒有開出存檔

#### Scenario: 審查不可用
- **WHEN** 草稿尚未審查，而模型層無法給出結果
- **THEN** 回應 503 與代碼 `review_unavailable`，沒有開出存檔
