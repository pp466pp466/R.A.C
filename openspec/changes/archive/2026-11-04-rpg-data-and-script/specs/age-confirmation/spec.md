# Spec Delta

## ADDED Requirements

### Requirement: 年齡確認時決定預設內容模式
年齡確認成功時，系統 SHALL 依確認當下由生日算出的年齡設定使用者的內容模式偏好：已滿 18 歲為 `standard`，未滿 18 歲為 `safe`。之後偏好 SHALL NOT 因年齡增加而自動改變（滿 18 歲的使用者要自行切換才會調高，見 `content-tier-modes`）。年齡確認失敗（不一致、未滿 13 歲）SHALL NOT 留下任何偏好。

#### Scenario: 成年者確認
- **WHEN** 25 歲的新使用者以一致的生日與區間完成年齡確認
- **THEN** 內容模式偏好為 `standard`

#### Scenario: 未成年者確認
- **WHEN** 15 歲的新使用者完成年齡確認
- **THEN** 內容模式偏好為 `safe`

#### Scenario: 之後滿 18 歲
- **WHEN** 該 15 歲的使用者在三年後再次登入
- **THEN** 偏好仍是 `safe`，沒有自動調高

#### Scenario: 確認失敗不留偏好
- **WHEN** 年齡確認因生日與區間不一致而失敗
- **THEN** 沒有保存任何內容模式偏好

## REMOVED Requirements

### Requirement: 未成年者的安全模式與內容上限
**Reason**: 分級由 `sexual_content_level` 0 至 3 改為 `safe`、`standard`、`adult` 三種內容模式（AGENTS.md 第 7 節，2026-11-03），內容上限的規則整個換了語意，所以以新的能力取代，而不是在舊需求上修改。
**Migration**: 見 `content-tier-modes` 的「年齡上限」「生效的內容模式取最保守者」與「帳號頁對未成年者顯示安全模式的提示」。

### Requirement: 等級 3 預設關閉且只能以環境設定開啟
**Reason**: 等級 3 不再是一個使用者層級；成人模式的部署開關改名為 `ADULT_MODE_ENABLED`，且 staging 與 production 依 AGENTS.md 改為「明確設定才生效」，不再一律禁止。
**Migration**: 見 `content-tier-modes` 的「成人模式預設關閉且只能以環境設定開啟」與「舊的等級 3 環境變數已淘汰且不會開啟任何東西」。

### Requirement: 開啟等級 3 時須記錄許可涵蓋的模型
**Reason**: 同上，清單改名為 `ADULT_MODE_MODELS`，規則不變。
**Migration**: 見 `content-tier-modes` 的「開啟成人模式時須記錄許可涵蓋的模型」。
