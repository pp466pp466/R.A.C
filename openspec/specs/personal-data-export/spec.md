# personal-data-export Specification

## Purpose
讓使用者行使個資法的查詢與複製權：登入後可下載自己的帳號資料與全部對話，格式可攜且編碼正確，並確保匯出內容只含本人資料、不含任何憑證或內部欄位。

> 注意（2026-11-02）：AGENTS.md 第 10 節已修訂（Google 登入改版），change ① `google-first-login` 已實作（登入、年齡確認、登入有效期、帳號鎖定、申訴與管理員二階段登入），其需求於封存時同步進本規格；本規格中與玩家端密碼註冊、密碼登入、忘記密碼、信箱驗證有關的部分描述的是目前的實作，待 change ② 更新。

## Requirements

### Requirement: 登入使用者可匯出自己的資料
登入使用者 SHALL 能匯出自己的帳號資料與全部對話，格式為 JSON 或 CSV。端點 SHALL 需要登入，只匯出呼叫者本人的資料，且 SHALL NOT 接受任何指定其他使用者的參數。未登入 SHALL 回應 401。狀態為 `deletion_pending` 的受限登入狀態 SHALL NOT 能匯出。

#### Scenario: 匯出 JSON
- **WHEN** 登入使用者要求匯出 JSON
- **THEN** 回應一個附檔下載，內容為該使用者的帳號資料與全部對話

#### Scenario: 未登入
- **WHEN** 未登入者要求匯出
- **THEN** 回應 401 的 JSON 錯誤

#### Scenario: 不能匯出別人的資料
- **WHEN** 登入使用者在請求中夾帶其他使用者的識別碼
- **THEN** 該參數被忽略，匯出內容仍只屬於呼叫者

#### Scenario: 待刪除的受限狀態
- **WHEN** 狀態為 `deletion_pending` 的使用者要求匯出
- **THEN** 回應 403，不產生任何內容

### Requirement: 匯出的內容範圍
帳號資料 SHALL 包含 email、顯示名稱、會員等級、信箱驗證時間、建立時間、最後登入時間、同意的條款版本與時間、生日、年齡區間、年齡確認時間與政策版本，以及是否已綁定 Google。每則對話 SHALL 包含標題、摘要、建立與最後活動時間；每則訊息 SHALL 包含序號、角色、內容與時間。匯出 SHALL NOT 含密碼雜湊、`token_version`、驗證權杖、第三方身分的識別值、內部整數編號，以及意圖、實體、信心值、建議動作與對話狀態等模型內部欄位。

#### Scenario: 含生日與年齡區間
- **WHEN** 已完成年齡確認的使用者匯出
- **THEN** 帳號資料包含其生日與年齡區間

#### Scenario: 不含憑證與內部欄位
- **WHEN** 使用者匯出
- **THEN** 內容中找不到密碼雜湊、權杖、第三方識別值、內部整數編號與模型內部欄位

#### Scenario: 訊息順序
- **WHEN** 對話有多則訊息
- **THEN** 訊息依序號由小到大排列

### Requirement: JSON 格式與編碼
JSON 匯出 SHALL 為 UTF-8（無 BOM），繁體中文 SHALL 以原字元輸出而非跳脫碼，且 SHALL 可被標準 JSON 解析器無損往返。檔名 SHALL 為純 ASCII。

#### Scenario: 中文往返
- **WHEN** 對話內容含繁體中文，匯出 JSON 後再解析
- **THEN** 內容與原文逐字相同，檔案開頭沒有 BOM

### Requirement: CSV 格式與編碼
CSV 匯出 SHALL 為一個 zip 檔，內含 `account.csv`（帳號資料，一列）與 `messages.csv`（每則訊息一列，含所屬對話的標題與時間）。兩個檔案 SHALL 為 UTF-8-sig，使 Excel 開啟繁體中文不亂碼。儲存格內容以 `=`、`+`、`-`、`@`、Tab 或歸位字元開頭時 SHALL 在前面加單引號，避免被試算表當成公式執行。

#### Scenario: Excel 可讀
- **WHEN** 使用者匯出 CSV
- **THEN** zip 內兩個檔案都以 UTF-8 BOM 開頭，繁體中文可無損解碼

#### Scenario: 公式字元被中和
- **WHEN** 某則訊息內容以 `=` 開頭
- **THEN** `messages.csv` 中該儲存格以單引號開頭，不會被當成公式

#### Scenario: 含逗號與換行
- **WHEN** 訊息內容含逗號、雙引號與換行
- **THEN** 以 CSV 規則正確引用，解析後內容相同

### Requirement: 匯出回應不可被快取且只記錄事件
匯出回應 SHALL 禁止快取（`no-store`）並以附件方式下載。每次成功的匯出 SHALL 記錄一筆結構化日誌，只含事件名稱、內部使用者編號與格式（列舉值），SHALL NOT 含任何匯出的內容、email 或生日。

#### Scenario: 回應標頭
- **WHEN** 使用者匯出
- **THEN** 回應禁止快取且標示為附件

#### Scenario: 日誌不含內容
- **WHEN** 使用者匯出含生日與對話的資料並收集日誌
- **THEN** 日誌只有事件名稱、內部編號與格式，不含生日、email 與對話內容
