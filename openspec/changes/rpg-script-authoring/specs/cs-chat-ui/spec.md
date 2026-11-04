# Spec Delta

## MODIFIED Requirements

### Requirement: 被閘門擋下時引導使用者
客服 API 回應尚未完成年齡確認、信箱未驗證、帳號被鎖定或服務暫時無法使用時，頁面 SHALL 以繁體中文說明原因；尚未完成年齡確認 SHALL 附上前往帳號設定補填的連結，帳號被鎖定（`account_locked`）SHALL 附上「前往申訴」按鈕連到申訴頁，且輸入區 SHALL 停用。

#### Scenario: 尚未填生日
- **WHEN** 沒有生日的帳號開啟 `/cs`
- **THEN** 顯示請先填寫生日的說明與前往帳號設定的連結，無法送出訊息

#### Scenario: 服務不可用
- **WHEN** API 回應 503
- **THEN** 顯示服務暫時無法使用的提示，已輸入的文字保留

#### Scenario: 使用中被鎖定
- **WHEN** 玩家在已開啟的 `/cs` 送出訊息，API 回應 403 與代碼 `account_locked`
- **THEN** 顯示帳號已被鎖定的說明與「前往申訴」按鈕，輸入區停用，已輸入的文字保留
