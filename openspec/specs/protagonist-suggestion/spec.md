# protagonist-suggestion Specification

## Purpose
定義 AI 生成主角預覽：玩家可寫選填的自我介紹，請 AI 依劇本的背景故事生成一張主角角色卡與一個錨定事實；生成只是無狀態的預覽，不寫入存檔，自我介紹不落地，失敗時改走自創，並有每日次數上限與用量帳本。讓玩家快速起手又不犧牲安全與個資。

## Requirements

### Requirement: 生成預覽不寫入存檔也不寫入資料庫
`POST /api/sessions/protagonist/suggest` SHALL 回傳一張主角角色卡與一個錨定事實的候選，以及今日剩餘次數。這支 API SHALL 是無狀態的預覽：SHALL NOT 建立或修改存檔、劇本或使用者資料，唯一的資料庫寫入是用量帳本。回應 SHALL 為 `Cache-Control: no-store`。

#### Scenario: 成功的預覽
- **WHEN** 玩家對上架的劇本請求生成
- **THEN** 回應含合法的角色卡與錨定事實，以及剩餘次數

#### Scenario: 不留下存檔
- **WHEN** 生成成功
- **THEN** 該玩家的存檔數量不變，劇本與使用者資料沒有任何改動

#### Scenario: 可見性
- **WHEN** 玩家對別人的草稿版本請求生成
- **THEN** 回應 404

#### Scenario: 劇本分級高於玩家模式
- **WHEN** 安全模式的玩家對宣告為一般模式的劇本請求生成
- **THEN** 回應 403 與代碼 `content_tier_restricted`，沒有呼叫模型也不計次

### Requirement: 自我介紹是選填的使用者文字，只作為資料且不保存
請求 MAY 帶自我介紹（NFC 標準化並去除控制字元，至多 300 字）；沒有自我介紹時 SHALL 只依背景故事生成。自我介紹 SHALL 只放在使用者內容區的資料區塊，SHALL NOT 進入 `system_instruction`，其中要求改變規則的文字 SHALL NOT 生效。系統 SHALL NOT 把自我介紹寫入資料庫、日誌或回應。

#### Scenario: 沒有自我介紹
- **WHEN** 玩家不填自我介紹請求生成
- **THEN** 回應合法的候選

#### Scenario: 自我介紹過長
- **WHEN** 自我介紹超過 300 字
- **THEN** 回應 400，欄位錯誤指出 `intro`，沒有呼叫模型

#### Scenario: 指令在資料區塊
- **WHEN** 檢查送給模型的請求
- **THEN** 自我介紹只出現在使用者內容區，`system_instruction` 中沒有它的任何文字

#### Scenario: 不落地
- **WHEN** 生成流程結束後檢查資料庫、日誌與回應
- **THEN** 找不到自我介紹的文字

### Requirement: 呼叫模型之前先過規則層
請求的自我介紹 SHALL 先通過規則層；結果為 `reject` 時 SHALL 以 422 `protagonist_rejected` 拒絕並只告知類別，SHALL NOT 呼叫模型，也 SHALL NOT 計入生成次數。模型回傳的角色卡與錨定事實 SHALL 同樣通過規則層，未通過視為驗證失敗（走重試）。

#### Scenario: 自我介紹被規則層拒絕
- **WHEN** 自我介紹的同一句話同時有露骨性用語與未成年用語
- **THEN** 回應 422 與代碼 `protagonist_rejected`，沒有呼叫模型，剩餘次數不變

#### Scenario: 模型輸出不合規則
- **WHEN** 模型的輸出含有被規則層直接拒絕的內容
- **THEN** 視為驗證失敗，依重試規則處理

### Requirement: 模型輸出由單一函式解析並經角色與錨定事實驗證
生成 SHALL 以 RPG 用途的模型（`OPENROUTER_MODEL`，備援 `OPENROUTER_FALLBACK_MODEL`，不使用 Gemini）、非串流、結構化輸出，沿用既有的解析、重試與備援規則。輸出 SHALL 通過角色卡驗證與錨定事實驗證（含指向存在、未成年戀愛限制）；輸出含系統不公開的標記字串 SHALL 視為驗證失敗。重試後仍失敗、降級或被供應商過濾 SHALL 回 503 `suggestion_unavailable`。

#### Scenario: 輸出合法
- **WHEN** 模型回傳合法的 JSON，角色卡與錨定事實通過驗證
- **THEN** 回應該候選

#### Scenario: 第一次不合法、第二次合法
- **WHEN** 模型第一次回傳指向不存在人物的錨定事實，重試後合法
- **THEN** 回應重試後的候選，帳本有兩筆紀錄

#### Scenario: 兩次都不合法
- **WHEN** 模型兩次都回傳不合法的輸出
- **THEN** 回應 503 與代碼 `suggestion_unavailable`，訊息提示可自行創角

#### Scenario: 含不公開標記
- **WHEN** 模型的輸出含有不公開的標記字串
- **THEN** 視為驗證失敗，候選不回給玩家

#### Scenario: 模型沒有設定
- **WHEN** RPG 用途的模型沒有設定
- **THEN** 回應 503 與代碼 `suggestion_unavailable`，不計入次數

### Requirement: 提示詞遵守內容分級與年齡規則
提示詞 SHALL 為繁體中文，內容模式採存檔將使用的模式（玩家生效模式與劇本宣告中較保守者），並要求：生成的內容不得超出該模式；不得涉及未成年的性或戀愛設定；錨定事實必須指向劇本中存在的項目；自我介紹提到年齡時角色年齡須相符。劇本的背景、人物、地點與里程碑文字同樣是資料，放在資料區塊。

#### Scenario: 安全模式
- **WHEN** 安全模式的玩家請求生成
- **THEN** 提示詞的內容模式為安全，輸出不含性或露骨內容的設定

#### Scenario: 劇本文字是資料
- **WHEN** 檢查送給模型的請求
- **THEN** 劇本的背景與人物文字只在資料區塊，不在 `system_instruction`

### Requirement: 生成與重新生成有每日次數上限
每位玩家每個台灣日的生成次數 SHALL 受上限約束，上限依會員等級，存放於資料庫設定表並每次現讀；用盡後 SHALL 以 429 `suggestion_limit` 拒絕且 SHALL NOT 呼叫模型。只有實際呼叫了模型的請求 SHALL 計次（以帳本中第一次嘗試的筆數計）；被規則層拒絕、模型未設定、預算用盡與其他未呼叫模型的請求 SHALL NOT 計次。不認得的會員等級 SHALL 套用 free。

#### Scenario: 用盡後拒絕
- **WHEN** free 玩家今日的生成次數已達上限後再次請求
- **THEN** 回應 429 與代碼 `suggestion_limit`，沒有呼叫模型

#### Scenario: 重試不重複計次
- **WHEN** 一次請求因驗證失敗重試了模型
- **THEN** 只計一次

#### Scenario: 未呼叫模型不計次
- **WHEN** 請求被規則層拒絕
- **THEN** 今日剩餘次數不變

#### Scenario: 隔日重置
- **WHEN** 台灣日界過後
- **THEN** 玩家的剩餘次數恢復

#### Scenario: 修改立即生效
- **WHEN** 部署者調高 `protagonist_suggest_daily.free` 後玩家下一次請求
- **THEN** 依新的上限判斷

### Requirement: 生成呼叫進入用量帳本，計入全站預算但不計輪數
每次生成呼叫 SHALL 寫入用量帳本，呼叫類別為 `protagonist_gen`，記錄實際使用的模型、嘗試次數、用量與結果狀態；其 token SHALL 計入全站每日上限，SHALL NOT 計入每日對話輪數。全站預算已用盡時 SHALL 回 503 `suggestion_unavailable` 且不呼叫模型。帳本與日誌 SHALL NOT 含自我介紹、角色卡或模型原文（驗證失敗的原文依既有規則截斷保存於 `error_detail`，僅管理員可讀）。

#### Scenario: 帳本
- **WHEN** 一次生成呼叫完成
- **THEN** 帳本有一筆類別為 `protagonist_gen` 的紀錄，含模型名稱與用量

#### Scenario: 不影響對話額度
- **WHEN** 玩家用完今日的生成次數
- **THEN** 其每日對話輪數的剩餘量不變

#### Scenario: 預算用盡
- **WHEN** 全站每日 token 已達上限
- **THEN** 回應 503 與代碼 `suggestion_unavailable`，沒有呼叫模型

#### Scenario: 日誌
- **WHEN** 生成流程收集日誌
- **THEN** 日誌只含事件名稱、內部編號、模型名稱、整數長度與列舉
