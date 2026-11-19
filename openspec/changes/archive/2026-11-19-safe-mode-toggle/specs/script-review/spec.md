# Spec Delta

## MODIFIED Requirements

### Requirement: 一般的未成年角色不得被誤擋
規則層 SHALL 只在同一句內同時出現性相關詞與明確的未成年詞（或小於 18 的年齡數字）時，才以 `minor_sexual` 直接拒絕。未綁定安全模式（`adult`，含舊值 `standard`）的劇本，若有年齡小於 18 的角色且該角色的文字出現性相關詞，結果 SHALL 為 `flag` 的 `minor_suspected`，不直接拒絕。親子、育兒與校園日常等不含性相關詞的未成年角色 SHALL 通過。

#### Scenario: 帶著三歲寶寶的主角
- **WHEN** 劇本的角色是一位成年主角與他三歲的孩子，內容為日常生活
- **THEN** 結果為 `pass`

#### Scenario: 校園日常
- **WHEN** 劇本的角色是十六歲的高中生，內容為社團與考試
- **THEN** 結果為 `pass`

#### Scenario: 同句的明確命中
- **WHEN** 同一句同時出現未滿 18 歲的年齡與性行為的描述
- **THEN** 結果為 `reject`，類別為 `minor_sexual`

#### Scenario: 未成年角色加性相關詞
- **WHEN** 未綁定安全模式的劇本有 17 歲的角色，其個性欄位出現性相關詞，但沒有同句的明確命中
- **THEN** 結果為 `flag`，類別為 `minor_suspected`
