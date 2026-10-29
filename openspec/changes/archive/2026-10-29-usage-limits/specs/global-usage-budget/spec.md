# Spec Delta

## Purpose

定義全站每日用量上限：以模型呼叫帳本彙總當日的 token 與呼叫次數，接近上限時記錄警告，達到上限時停止呼叫模型並以系統忙碌文案回覆，使成本與供應商的每日配額有一道不依賴個別使用者的保護。

## ADDED Requirements

### Requirement: 全站每日上限的設定
全站每日 token 上限 SHALL 由環境變數 `USAGE_DAILY_TOKEN_LIMIT` 設定，全站每日呼叫次數上限 SHALL 由 `USAGE_DAILY_CALL_LIMIT` 設定，兩者皆為正整數；未設定時 SHALL 使用保守的預設值。設定為非正整數、非數字或超過合理上限時 SHALL 讓應用程式在啟動時失敗並指出是哪個變數。

#### Scenario: 未設定
- **WHEN** 沒有設定這兩個變數
- **THEN** 應用程式使用預設值啟動

#### Scenario: 不合法的值
- **WHEN** `USAGE_DAILY_TOKEN_LIMIT` 為 `0`，或 `USAGE_DAILY_CALL_LIMIT` 為 `-5` 或 `abc`
- **THEN** 啟動失敗，訊息指出是該變數

### Requirement: 以帳本彙總當日用量
系統 SHALL 以 `llm_call_logs` 彙總當日（Asia/Taipei 日界）全部模型呼叫：token 為輸入、輸出與思考 token 的總和，呼叫次數為帳本的筆數；兩者都包含降級、摘要與失敗的嘗試。供應商沒有回報 token 的紀錄 SHALL 以 0 計。用量 SHALL 不依賴行程內記憶體，使多個 worker 看到同一個數字。

#### Scenario: 包含所有種類的呼叫
- **WHEN** 今日有對話呼叫 1,000 token、摘要呼叫 400 token、一次失敗嘗試 200 token
- **THEN** 全站 token 用量為 1,600，呼叫次數為 3

#### Scenario: 空值
- **WHEN** 某筆紀錄的 token 欄位為空值
- **THEN** 該筆以 0 計 token、仍計為一次呼叫，彙總不出錯

#### Scenario: 跨日歸零
- **WHEN** 台灣時間進入新的一天
- **THEN** 前一日的紀錄不再計入

### Requirement: 任一上限達到時停止呼叫模型
送出訊息時，全站今日的 token 用量或呼叫次數任一項達到或超過其上限，系統 SHALL 回應 503 與代碼 `system_busy`，文案為「目前使用人數較多，請稍後再試。」，並 SHALL NOT 儲存訊息、設定處理標記或呼叫模型。回應文案 SHALL NOT 透露上限或用量的數字。

#### Scenario: token 達到上限
- **WHEN** 全站今日 token 用量已等於上限，使用者送出訊息
- **THEN** 回應 503、代碼 `system_busy`、上述繁體中文文案；沒有新增訊息或帳本紀錄

#### Scenario: 呼叫次數達到上限
- **WHEN** 全站今日呼叫次數已等於上限，而 token 用量遠低於上限
- **THEN** 同樣回應 503 `system_busy`

#### Scenario: 已處理的訊息仍可取回
- **WHEN** 全站用量已達上限，使用者重送一則已有回覆的訊息
- **THEN** 回應既有回覆

#### Scenario: 不洩漏數字
- **WHEN** 檢視 `system_busy` 的回應
- **THEN** 內容不含上限或用量

### Requirement: 任一項達 80% 記錄警告
送出訊息時，全站今日的 token 用量或呼叫次數任一項達到其上限的 80% 以上而兩項都尚未達到上限，系統 SHALL 記錄一筆警告事件，包含是哪一項、目前用量與上限，供後續的告警使用；事件 SHALL NOT 阻擋該則訊息。

#### Scenario: 越過 80%
- **WHEN** 全站呼叫次數為上限的 85% 時有使用者送出訊息
- **THEN** 該則訊息照常處理，日誌出現一筆含用量與上限數字的警告事件

#### Scenario: 低於 80%
- **WHEN** 兩項用量都低於其上限的 80%
- **THEN** 不記錄警告事件

### Requirement: 摘要折疊在預算用完時延後
背景的對話摘要折疊也是模型呼叫。全站今日任一項用量達到上限時，摘要折疊 SHALL 延後而不呼叫模型；對話本身 SHALL NOT 受影響，之後的請求會補做。

#### Scenario: 預算用完
- **WHEN** 回覆提交後觸發摘要折疊，但全站用量已達上限
- **THEN** 不呼叫模型，該對話的已摘要序號不變，對話可繼續使用
