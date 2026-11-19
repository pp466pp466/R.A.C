# Spec Delta

## MODIFIED Requirements

### Requirement: 目錄與設定欄位的長度與必填
下列欄位 SHALL 依規定驗證長度（以 Unicode 字元計，標準化之後）：`title` 必填 1 至 30 字、`summary` 必填 1 至 150 字、`world_name` 必填 1 至 50 字、`background` 必填 1 至 1500 字、`opening` 選填至多 500 字、`rules` 選填至多 800 字。`tags` 選填，至多 5 個，每個 1 至 12 字，以 NFC 與不分大小寫去除重複。`content_tier` 必填，只能是 `safe`（綁定安全模式）或 `adult`（未綁定）；舊值 `standard` SHALL 接受並標準化為 `adult`。超過上限或缺少必填欄位 SHALL 回報該欄位的錯誤，錯誤訊息 SHALL 為繁體中文且不回顯輸入的內容。

#### Scenario: 標題超過上限
- **WHEN** 標題有 31 個字
- **THEN** 驗證失敗，錯誤指出 `title` 且訊息不含標題文字

#### Scenario: 背景為必填
- **WHEN** 沒有提供 `background`
- **THEN** 錯誤指出 `background` 為必填

#### Scenario: 標籤去重
- **WHEN** 標籤為 `仙俠`、`仙俠`、`Fantasy`、`fantasy`
- **THEN** 標準化後只剩 `仙俠` 與 `Fantasy` 各一個

#### Scenario: 標籤過多
- **WHEN** 提供 6 個不同的標籤
- **THEN** 驗證失敗，錯誤指出 `tags`

#### Scenario: 不合法的內容分級
- **WHEN** `content_tier` 為 `mature`
- **THEN** 驗證失敗，錯誤指出 `content_tier`

#### Scenario: 兩種合法值
- **WHEN** `content_tier` 為 `safe` 或 `adult`
- **THEN** 這個欄位通過驗證

#### Scenario: 舊值標準化
- **WHEN** `content_tier` 為 `standard`
- **THEN** 驗證通過，標準化後的內容為 `adult`

## REMOVED Requirements

### Requirement: 宣告分級不得高於作者的上限
**Reason**: 全站只限滿 18 歲，沒有依年齡而異的作者上限；內容模式改為「是否綁定安全模式」。
**Migration**: 驗證函式不再接受作者內容上限參數；呼叫端（創作、送審、開存檔把關）一併移除年齡上限的傳遞。
