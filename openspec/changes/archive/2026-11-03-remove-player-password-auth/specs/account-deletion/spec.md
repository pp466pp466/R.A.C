# Spec Delta

## ADDED Requirements

### Requirement: 申請後帳號立即停用並進入寬限期且不寄信
申請成功後，系統 SHALL 在同一個交易內把帳號狀態改為 `deletion_pending`，記錄申請時間與申請前的狀態，並使該帳號所有裝置的登入狀態失效（含目前這台）。系統 SHALL NOT 寄信給申請者（玩家端不寄任何信；寬限期與撤銷方式由申請成功後的畫面與 `/account` 說明）。申請前的狀態只可為 `active`；停權的帳號無法登入，因此不在自助刪除的範圍內。

#### Scenario: 申請成功
- **WHEN** 使用者申請刪除成功
- **THEN** 帳號狀態為 `deletion_pending`，申請時間與申請前的狀態已記錄，原本的登入狀態全部失效

#### Scenario: 不寄通知信
- **WHEN** 使用者申請刪除成功（不論有沒有寄信設定）
- **THEN** 沒有任何信件被寄出，申請結果與其餘行為不變

### Requirement: 寬限期內可撤銷刪除
狀態為 `deletion_pending` 且距申請時間未滿 7 天的帳號，其持有者以受限登入狀態呼叫撤銷時，系統 SHALL 將帳號還原為申請前的狀態（`active`）並清除申請時間與申請前的狀態。超過 7 天 SHALL 回應 409 與代碼 `grace_expired`，帳號維持待刪除。

#### Scenario: 撤銷
- **WHEN** 待刪除的使用者在第 3 天撤銷
- **THEN** 帳號回到申請前的狀態，之後可正常使用

#### Scenario: 逾期
- **WHEN** 待刪除的使用者在申請滿 7 天之後才撤銷
- **THEN** 回應 409 與 `grace_expired`，帳號維持待刪除

## REMOVED Requirements

### Requirement: 寬限期內可撤銷
**Reason**: 原需求含 `pending_verification` 的還原情境；該狀態已退役，撤銷一律還原為 `active`。
**Migration**: 見「寬限期內可撤銷刪除」。

### Requirement: 申請後帳號立即停用並進入寬限期
**Reason**: 原需求在信箱驗證開啟時寄通知信，並含 `pending_verification` 的申請前狀態；玩家端不再寄任何信，該狀態已退役。
**Migration**: 見「申請後帳號立即停用並進入寬限期且不寄信」。
