# Spec Delta

## Purpose
定義劇本固定規格 v1：所有劇本（內建與玩家創作）共用的欄位、長度與數量上限、列舉值與標準化規則，以及驗證失敗時逐欄位的錯誤。它是創作、審核、開存檔與回合引擎共用的資料合約，與儲存方式、審核與介面無關。

## ADDED Requirements

### Requirement: 規格版本與驗證結果
系統 SHALL 提供單一的劇本內容驗證，輸入為劇本內容，輸出為標準化後的內容（含規格版本 `spec_version`，目前為 1，與內容的雜湊 `content_hash`）或逐欄位的錯誤。驗證 SHALL 為純運算：沒有資料庫、網路與時間的依賴，相同輸入 SHALL 得到相同輸出。內容含規格未定義的欄位 SHALL 被拒絕，而不是被忽略（標準化後的輸出帶有的 `spec_version` 除外：它必須等於目前的版本，這使輸出可以被再驗證一次而得到相同的結果）。多個欄位同時有錯 SHALL 一次回報全部，不是只報第一個。

#### Scenario: 合法內容
- **WHEN** 驗證一份欄位齊全且在上限之內的劇本內容
- **THEN** 回傳標準化內容，`spec_version` 為 1，並附 `content_hash`

#### Scenario: 相同輸入相同輸出
- **WHEN** 對同一份內容驗證兩次
- **THEN** 標準化內容與 `content_hash` 完全相同

#### Scenario: 未定義的欄位
- **WHEN** 內容含有規格沒有的欄位，例如 `custom_prompt`
- **THEN** 驗證失敗，錯誤指出該欄位名稱

#### Scenario: 標準化內容可再被驗證
- **WHEN** 把標準化後的輸出再驗證一次
- **THEN** 得到相同的標準化內容與 `content_hash`；若其中的 `spec_version` 不是目前的版本則驗證失敗，錯誤指出 `spec_version`

#### Scenario: 多個錯誤一次回報
- **WHEN** 標題為空，且第三位角色的年齡不是整數
- **THEN** 回傳的錯誤同時包含 `title` 與 `characters[2].age`

### Requirement: 目錄與設定欄位的長度與必填
下列欄位 SHALL 依規定驗證長度（以 Unicode 字元計，標準化之後）：`title` 必填 1 至 30 字、`summary` 必填 1 至 150 字、`world_name` 必填 1 至 50 字、`background` 必填 1 至 1500 字、`opening` 選填至多 500 字、`rules` 選填至多 800 字。`tags` 選填，至多 5 個，每個 1 至 12 字，以 NFC 與不分大小寫去除重複。`content_tier` 必填，只能是 `safe`、`standard`、`adult` 之一。超過上限或缺少必填欄位 SHALL 回報該欄位的錯誤，錯誤訊息 SHALL 為繁體中文且不回顯輸入的內容。

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

### Requirement: 文字標準化
所有文字欄位 SHALL 先做 NFC 正規化並去除控制字元：單行欄位（標題、簡介、世界觀名稱、標籤、姓名等）SHALL 另外把換行與連續空白收斂為單一空白並去除前後空白；多行欄位（背景、初始對話、規則、結局說明、外貌等較長欄位）SHALL 保留換行但去除前後空白，且不得含歸位字元以外的控制字元。文字 SHALL NOT 含系統不公開的標記字串（驗證時由呼叫端提供清單）。全部以純文字處理，驗證 SHALL NOT 解讀任何標記語法。

#### Scenario: 全形與組合字元
- **WHEN** 標題使用 NFD 組合字元輸入
- **THEN** 標準化後為 NFC，長度以標準化後計算

#### Scenario: 控制字元
- **WHEN** 簡介含有零寬度控制字元與 `\u0007`
- **THEN** 標準化後這些字元被移除

#### Scenario: 單行欄位收斂換行
- **WHEN** 姓名含換行與兩個連續空白
- **THEN** 標準化後為單一空白分隔的一行

#### Scenario: 不公開標記字串
- **WHEN** 呼叫端提供一個標記字串，而背景文字含有它
- **THEN** 驗證失敗，錯誤指出 `background`，訊息不顯示該標記字串

### Requirement: 角色
`characters` SHALL 為 0 至 10 位角色。每位角色的 `name`（1 至 20 字）、`personality`（1 至 60 字）、`age`（0 至 9999 的整數）與 `appearance`（1 至 150 字）SHALL 為必填；`occupation`（至多 20 字）、`speech_style`（至多 100 字）與 `hidden_trait`（至多 60 字）為選填。同一劇本內角色姓名 SHALL 唯一（以 NFC 與不分大小寫比較）。`age` SHALL 只接受整數，不接受文字、小數或布林；0 至 17 歲的角色是合法的（例如育兒題材的嬰幼兒）。

#### Scenario: 必填欄位
- **WHEN** 一位角色沒有 `appearance`
- **THEN** 驗證失敗，錯誤指出 `characters[0].appearance`

#### Scenario: 年齡必須是整數
- **WHEN** 角色的 `age` 為 `十八歲`
- **THEN** 驗證失敗，錯誤指出 `characters[0].age`

#### Scenario: 未成年角色合法
- **WHEN** 劇本含一位 `age` 為 3 的角色，其餘欄位合法
- **THEN** 驗證通過

#### Scenario: 姓名重複
- **WHEN** 兩位角色的姓名在標準化後相同
- **THEN** 驗證失敗，錯誤指出重複的那一位的 `name`

#### Scenario: 角色過多
- **WHEN** 提供 11 位角色
- **THEN** 驗證失敗，錯誤指出 `characters`

### Requirement: 地點
`locations` SHALL 為 0 至 15 個地點名稱，每個 1 至 30 字，以標準化後去除重複。地點可為空，遊戲中新增的地點只屬於存檔，SHALL NOT 回寫到劇本。

#### Scenario: 地點可為空
- **WHEN** 沒有提供 `locations`
- **THEN** 驗證通過，標準化後為空清單

#### Scenario: 地點重複
- **WHEN** 地點為 `古劍山莊` 與 `古劍山莊`
- **THEN** 標準化後只剩一個

### Requirement: 里程碑與曆法
`milestones` SHALL 為 0 至 8 個。每個里程碑 SHALL 有 `title`（1 至 60 字）與結構化的 `trigger`：`kind` 只能是 `age`（玩家年齡達到）或 `year`（遊戲內年份達到），`value` 為整數；`note` 為選填至多 100 字。系統 SHALL 為每個里程碑確保有穩定的 `id`（未提供時依順序產生，同一份內容內唯一），之後同一個里程碑在新版本中沿用相同的 `id`。只要有任何一個 `year` 觸發，劇本 SHALL 提供曆法 `calendar`（`name` 1 至 12 字、`start_year` 整數），且該觸發的 `value` SHALL 不早於 `start_year`。觸發 SHALL NOT 以自由文字表達。

#### Scenario: 年份觸發需要曆法
- **WHEN** 有一個 `year` 觸發，但沒有 `calendar`
- **THEN** 驗證失敗，錯誤指出 `calendar`

#### Scenario: 年份不得早於起始年
- **WHEN** 曆法起始年為 1210，而某里程碑的年份觸發為 1205
- **THEN** 驗證失敗，錯誤指出該里程碑的 `trigger.value`

#### Scenario: 年齡觸發不需要曆法
- **WHEN** 里程碑為「20 歲宗門大測」，觸發為 `age` 20，且沒有曆法
- **THEN** 驗證通過

#### Scenario: 觸發不接受自由文字
- **WHEN** 里程碑的觸發為 `乾歷朝1215年`
- **THEN** 驗證失敗，錯誤指出該里程碑的 `trigger`

#### Scenario: 穩定的編號
- **WHEN** 兩個里程碑都沒有提供 `id`
- **THEN** 標準化後各有不同的 `id`，且對相同輸入重複驗證得到相同的 `id`

### Requirement: 結局
`ending_mode` SHALL 為 `none`（無結局）或 `custom`（自訂結局），未提供時為 `none`。`custom` 時 SHALL 提供 `ending_text`（1 至 300 字）並可提供 `ending_age`（1 至 9999 的整數，由系統判定）；`none` 時 SHALL NOT 同時提供 `ending_text` 或 `ending_age`。結局模式 SHALL 只由 `ending_mode` 決定，不由 `ending_text` 的文字內容推測。

#### Scenario: 預設無結局
- **WHEN** 沒有提供結局欄位
- **THEN** 標準化後 `ending_mode` 為 `none`

#### Scenario: 自訂結局需要說明
- **WHEN** `ending_mode` 為 `custom` 但沒有 `ending_text`
- **THEN** 驗證失敗，錯誤指出 `ending_text`

#### Scenario: 無結局不得附說明
- **WHEN** `ending_mode` 為 `none` 但提供了 `ending_text`
- **THEN** 驗證失敗，錯誤指出 `ending_text`

#### Scenario: 文字不決定模式
- **WHEN** `ending_mode` 為 `custom`，而 `ending_text` 寫著「不要有固定結局」
- **THEN** 結局模式仍為 `custom`

### Requirement: 圖片欄位預留
角色 SHALL 有預留的 `image` 欄位，劇本 SHALL 有預留的 `cover_image` 欄位。v1 SHALL 只接受空值：任何非空值（網址、檔名、資料 URI 等）SHALL 使驗證失敗，錯誤指出該欄位。

#### Scenario: 空值
- **WHEN** 角色的 `image` 為空或沒有提供
- **THEN** 驗證通過，標準化後為空值

#### Scenario: 非空值被拒絕
- **WHEN** 角色的 `image` 為 `https://example.com/a.png`
- **THEN** 驗證失敗，錯誤指出 `characters[0].image`，且內容不會被保存

### Requirement: 總量上限
所有文字欄位（含標籤、角色、地點、里程碑與結局）標準化後的字元總數 SHALL 不超過 8000 字，數字欄位不計入。超過時 SHALL 回報 `total` 的錯誤，並指出目前的總字數與上限，而不只是指出某一個欄位。

#### Scenario: 超過總量上限
- **WHEN** 各欄位都在自己的上限之內，但全部加總為 8200 字
- **THEN** 驗證失敗，錯誤指出 `total`，並說明超過上限

#### Scenario: 剛好在上限
- **WHEN** 加總剛好 8000 字
- **THEN** 驗證通過

### Requirement: 宣告分級不得高於作者的上限
驗證 SHALL 接受呼叫端提供的作者內容上限；宣告的 `content_tier` 高於該上限 SHALL 使驗證失敗，錯誤指出 `content_tier`。未成年作者的上限為 `safe`。

#### Scenario: 未成年作者宣告一般
- **WHEN** 作者的上限為 `safe`，內容宣告 `content_tier` 為 `standard`
- **THEN** 驗證失敗，錯誤指出 `content_tier`

#### Scenario: 成年作者宣告一般
- **WHEN** 作者的上限為 `standard`，內容宣告 `content_tier` 為 `standard`
- **THEN** 驗證通過

### Requirement: 錯誤格式
驗證錯誤 SHALL 以欄位路徑為鍵（例如 `title`、`characters[2].age`、`milestones[0].trigger.value`），值為繁體中文訊息；訊息 SHALL NOT 回顯使用者輸入的內容。路徑 SHALL 能被前端直接對應到表單欄位。

#### Scenario: 路徑格式
- **WHEN** 第一個里程碑的觸發值不是整數
- **THEN** 錯誤的鍵為 `milestones[0].trigger.value`

#### Scenario: 訊息不回顯輸入
- **WHEN** 任何欄位驗證失敗
- **THEN** 錯誤訊息中找不到使用者輸入的文字
