# Spec Delta

## ADDED Requirements

### Requirement: 等級 3 預設關閉且只能以環境設定開啟
`sexual_content_level` 3 SHALL 由環境變數 `SEXUAL_CONTENT_LEVEL3_ENABLED` 控制，預設為關閉，SHALL NOT 在程式中寫死為開啟。`development` 與 `testing` 可開啟；`staging` 與 `production` 在強化的成年驗證完成之前設定為開啟 SHALL 使應用程式啟動失敗，訊息只指出變數名稱。值不是布林時 SHALL 啟動失敗。

#### Scenario: 沒有設定
- **WHEN** 沒有設定 `SEXUAL_CONTENT_LEVEL3_ENABLED`
- **THEN** 等級 3 為關閉

#### Scenario: 開發環境開啟
- **WHEN** `APP_ENV` 為 `development` 且開關設為開啟，其他條件齊備
- **THEN** 應用程式可啟動，等級 3 為開啟

#### Scenario: 預備與正式環境
- **WHEN** `APP_ENV` 為 `staging` 或 `production` 且開關設為開啟
- **THEN** 啟動失敗，訊息指出 `SEXUAL_CONTENT_LEVEL3_ENABLED`，說明尚未完成成年驗證

#### Scenario: 值不合法
- **WHEN** 開關的值是 `maybe`
- **THEN** 應用程式啟動失敗，訊息指出變數名稱而不含其值

### Requirement: 開啟等級 3 時須記錄許可涵蓋的模型
開啟等級 3 時，`SEXUAL_CONTENT_LEVEL3_MODELS`（逗號分隔的模型識別碼，代表取得書面許可所涵蓋的模型）SHALL 非空，且 RPG 用途的主要模型與備援模型 SHALL 全部在清單內，RPG 用途 SHALL 已設定；任一條件不成立 SHALL 使應用程式啟動失敗，訊息只指出變數名稱。清單 SHALL NOT 影響開關關閉時的行為。

#### Scenario: 清單涵蓋模型
- **WHEN** 開關開啟，RPG 用途的主要模型在 `SEXUAL_CONTENT_LEVEL3_MODELS` 內
- **THEN** 應用程式可啟動

#### Scenario: 模型不在清單內
- **WHEN** 開關開啟，但 RPG 用途的備援模型不在清單內
- **THEN** 應用程式啟動失敗，訊息指出 `SEXUAL_CONTENT_LEVEL3_MODELS`

#### Scenario: 清單為空
- **WHEN** 開關開啟且 `SEXUAL_CONTENT_LEVEL3_MODELS` 沒有設定
- **THEN** 應用程式啟動失敗，訊息指出 `SEXUAL_CONTENT_LEVEL3_MODELS`

#### Scenario: 沒有設定 RPG 用途
- **WHEN** 開關開啟，但沒有設定 RPG 用途的供應商
- **THEN** 應用程式啟動失敗，訊息指出 RPG 用途尚未設定

## MODIFIED Requirements

### Requirement: 未成年者的安全模式與內容上限
內容上限 SHALL 由集中的權限政策依生日當下計算的年齡決定：未滿 18 歲者 SHALL 固定為安全模式（`sexual_content_level` 為 0，內容分級取最保守值），不論存檔旗標為何都不得提高；成年者的上限為 `sexual_content_level` 2，只有在「等級 3 開關」開啟時才為 3（見等級 3 的設定規則）。介面 SHALL 對未成年者顯示安全模式的提示。管理員與各會員等級 SHALL NOT 豁免。

#### Scenario: 未成年者
- **WHEN** 15 歲的使用者詢問內容上限
- **THEN** `sexual_content_level` 上限為 0，且標示為安全模式

#### Scenario: 成年者
- **WHEN** 25 歲的使用者詢問內容上限，且等級 3 開關為關閉（預設）
- **THEN** `sexual_content_level` 上限為 2，不會是 3

#### Scenario: 成年者，開關開啟
- **WHEN** 25 歲的使用者詢問內容上限，且等級 3 開關為開啟
- **THEN** `sexual_content_level` 上限為 3，且不是安全模式

#### Scenario: 開關開啟也不影響未成年者
- **WHEN** 等級 3 開關為開啟，15 歲的使用者詢問內容上限
- **THEN** 上限仍為 0，且標示為安全模式

#### Scenario: 尚未完成年齡確認
- **WHEN** 等級 3 開關為開啟，尚未填寫生日的使用者詢問內容上限
- **THEN** 視為安全模式，上限為 0

#### Scenario: 管理員不豁免
- **WHEN** 15 歲的管理員詢問內容上限
- **THEN** 同樣為安全模式

#### Scenario: 帳號設定頁的提示
- **WHEN** 13 至 17 歲的使用者開啟 `/account`
- **THEN** 頁面顯示目前為安全模式及其原因
