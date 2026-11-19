# Spec Delta

## MODIFIED Requirements

### Requirement: 玩家行動的規則層只擋確定的違規
行動文字 SHALL 通過規則層：命中必須拒絕的類別（涉及未成年的性內容、非自願的性情節、性暴力、以真人名義的性內容）時 SHALL 回應 422 與代碼 `input_blocked`，只帶類別，不回顯內容，也不預扣、不呼叫模型；此檢查不論生效模式一律執行。只因內容模式較保守而出現的「分級不符」SHALL NOT 阻擋行動（輸出層會依模式處理），系統 SHALL NOT 自動調高模式；行動被判為性內容而生效模式為安全開時，系統 SHALL 依 `safe-mode-upgrade-hint` 在生成之後提示玩家可以自行調整。

#### Scenario: 明確違規的行動
- **WHEN** 行動文字命中必須拒絕的類別
- **THEN** 回應 422 與代碼 `input_blocked`，回應只含類別，沒有預扣

#### Scenario: 安全模式下的曖昧行動
- **WHEN** 安全模式開的玩家送出一句曖昧的行動
- **THEN** 回合正常開始，生效模式不被調高

#### Scenario: 安全關仍擋明確違規
- **WHEN** 已關閉安全模式的玩家送出命中必須拒絕類別的行動
- **THEN** 回應 422 與代碼 `input_blocked`，沒有預扣
