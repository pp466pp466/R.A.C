# Spec Delta

## Purpose

定義 `/game/start/<版本識別碼>` 的開存檔精靈：序章、聯繫、創角與「開始冒險」的步驟與資料流，AI 生成預覽與自創、示範氣泡、錯誤與逾時的處理，以及創角內容不寫入瀏覽器儲存。精靈只呼叫既有的創角與開存檔 API，不改變任何驗證、審查或額度規則。

## ADDED Requirements

### Requirement: 精靈頁的外殼與創角資料載入
`/game/start/<版本識別碼>` SHALL 由 MVC Page Controller 渲染，需要登入，初始狀態 SHALL 只含閘門結果、會員等級與請求逾時設定（由設定算出，前端不寫死），不含劇本內容。前端 SHALL 以 `GET /api/sessions/setup/<id>` 載入序章與創角資料；404 SHALL 顯示「找不到這個劇本」並提供回 `/game` 的連結；403 `content_tier_restricted` SHALL 說明這個劇本的內容模式高於目前的模式並提供前往帳號設定的連結。載入期間 SHALL 顯示載入中，失敗 SHALL 顯示重試。

#### Scenario: 載入成功
- **WHEN** 玩家開啟上架劇本的精靈頁
- **THEN** 顯示序章（背景故事）與步驟指示

#### Scenario: 找不到
- **WHEN** 玩家開啟不存在或別人草稿的版本
- **THEN** 顯示「找不到這個劇本」與回 `/game` 的連結，沒有顯示任何劇本內容

#### Scenario: 分級高於玩家模式
- **WHEN** 安全模式的玩家開啟宣告為一般模式的劇本
- **THEN** 顯示內容模式高於目前模式的說明與前往帳號設定的連結，沒有進入創角

#### Scenario: 未登入
- **WHEN** 沒有登入就開啟精靈頁
- **THEN** 導向登入頁並帶回到該頁的返回位置

### Requirement: 序章與聯繫步驟
精靈 SHALL 依序有三個步驟並顯示進度：序章、聯繫、創角。序章 SHALL 唯讀顯示劇本標題與背景故事。聯繫 SHALL 讓玩家從劇本的人物、地點或里程碑中選一個對象，並自行寫一句關係（長度上限取自創角資料）；玩家 SHALL 可明確略過（略過時開場僅依背景故事與角色卡），選了對象卻沒寫內容 SHALL 在前端提示。人物只顯示姓名與職業，SHALL NOT 顯示年齡與隱藏特質。前端 SHALL NOT 自行判斷關係是否合規，合規驗證完全由伺服器負責。

#### Scenario: 序章唯讀
- **WHEN** 玩家在序章步驟
- **THEN** 只能閱讀背景故事並前進，沒有可編輯的欄位

#### Scenario: 選擇聯繫的對象
- **WHEN** 玩家選擇人物「某某」並寫下一句關係
- **THEN** 聯繫步驟完成，摘要顯示對象與那句關係

#### Scenario: 略過聯繫
- **WHEN** 玩家按「略過聯繫」並確認
- **THEN** 聯繫標示為略過，之後開存檔的請求帶 `skip_anchor: true` 而不帶錨定事實

#### Scenario: 選了對象沒寫內容
- **WHEN** 玩家選了對象但關係那句是空白，按下一步
- **THEN** 停在聯繫步驟並提示要寫一句或改為略過

#### Scenario: 人物不顯示隱藏資料
- **WHEN** 檢視聯繫步驟的人物選項
- **THEN** 只有姓名與職業

### Requirement: 創角步驟的欄位與示範氣泡
創角步驟 SHALL 提供主角角色卡的欄位（姓名、個性、年齡、外貌、職業、說話風格，以及創角資料列出的其他欄位）與來歷單選（本地人、穿越者，預設本地人），欄位的長度上限 SHALL 取自 `setup` 的回應並顯示剩餘字數。步驟 SHALL 顯示示範氣泡：以固定句型加玩家輸入的姓名與說話風格在前端渲染，不呼叫模型、不送出任何請求。頭像與性別欄位 SHALL NOT 出現。畫面 SHALL 說明年齡是劇中年齡、與玩家本人的年齡無關。

#### Scenario: 示範氣泡隨姓名更新
- **WHEN** 玩家輸入主角姓名
- **THEN** 示範氣泡即時顯示該姓名，且沒有任何網路請求

#### Scenario: 欄位上限
- **WHEN** 玩家在個性欄位輸入超過上限的文字
- **THEN** 顯示超出的提示，「開始冒險」禁用

#### Scenario: 來歷預設值
- **WHEN** 玩家進入創角步驟
- **THEN** 來歷預設為本地人，可改選穿越者

#### Scenario: 沒有頭像與性別
- **WHEN** 檢視創角步驟
- **THEN** 沒有頭像上傳與性別欄位

### Requirement: AI 生成預覽與自創
創角步驟 SHALL 有選填的自我介紹欄位（長度上限取自創角資料）與「請 AI 生成」按鈕，呼叫 `POST /api/sessions/protagonist/suggest`；成功時 SHALL 把回傳的角色卡與錨定事實填入表單作為**預覽**，玩家可修改後接受、重新生成，或改為自創；按鈕旁 SHALL 顯示今日剩餘次數，剩餘為 0 時 SHALL 禁用。已有手動填寫的內容時，生成前 SHALL 以自製 Modal 確認會覆蓋。回應失敗時 SHALL 保留玩家已填的內容並說明改為自創：429 顯示今日次數已用完，503 `suggestion_unavailable` 顯示暫時無法生成，422 只顯示審查類別而不指出內容。生成預覽 SHALL NOT 建立存檔。生成請求 SHALL 於離開頁面時以 AbortController 中止。

#### Scenario: 生成成功
- **WHEN** 玩家填了自我介紹並按「請 AI 生成」，伺服器回傳預覽
- **THEN** 表單填入角色卡與錨定事實，剩餘次數減少，資料庫沒有新增存檔

#### Scenario: 覆蓋前確認
- **WHEN** 表單已有內容時玩家按「請 AI 生成」
- **THEN** 先顯示自製 Modal 說明會覆蓋，確認後才送出請求

#### Scenario: 次數用完
- **WHEN** 今日剩餘次數為 0
- **THEN** 「請 AI 生成」為禁用，並說明可改為自創

#### Scenario: 生成不可用
- **WHEN** 伺服器回 503 `suggestion_unavailable`
- **THEN** 顯示暫時無法生成的說明，玩家已填的內容不變，可直接自創

#### Scenario: 被審查擋下
- **WHEN** 自我介紹被規則層拒絕（422）
- **THEN** 只顯示被拒絕的類別，不顯示或回顯自我介紹的內容

#### Scenario: 離開頁面時中止
- **WHEN** 生成進行中玩家離開頁面
- **THEN** 請求被中止

### Requirement: 開始冒險
創角步驟的「開始冒險」SHALL 以 `POST /api/sessions` 送出版本 `id`、角色卡（含來歷）與錨定事實或 `skip_anchor`；請求逾時 SHALL 取自頁面初始狀態（涵蓋兩層審查的最壞時間），並於離開頁面時以 AbortController 中止。送出期間 SHALL 禁用所有按鈕並顯示處理中，無論成功、失敗或中斷 SHALL 在 `finally` 恢復。成功（201）SHALL 導向 `/game/<新存檔的識別碼>`。失敗 SHALL 依代碼處理：400 把欄位錯誤顯示在對應欄位並跳回該步驟；422 `protagonist_rejected` 與 `script_rejected` 只顯示類別；503 `review_unavailable` 顯示請稍後再試；403 `content_tier_restricted`、`age_confirmation_required` 與其他閘門代碼顯示對應說明；所有失敗 SHALL 保留玩家已填的內容。同一時間 SHALL 只送出一個開存檔請求。

#### Scenario: 成功
- **WHEN** 合法的內容送出且伺服器回 201
- **THEN** 導向 `/game/<新存檔的識別碼>`

#### Scenario: 欄位錯誤
- **WHEN** 伺服器回 400 並指出 `protagonist.age`
- **THEN** 錯誤顯示在年齡欄位旁，畫面停在創角步驟，已填內容不變

#### Scenario: 被審查擋下
- **WHEN** 伺服器回 422 `protagonist_rejected` 並帶類別
- **THEN** 只顯示類別的說明，不顯示或回顯任何玩家文字，按鈕恢復可用

#### Scenario: 審查暫時不可用
- **WHEN** 伺服器回 503 `review_unavailable`
- **THEN** 顯示請稍後再試，按鈕恢復可用，沒有導向

#### Scenario: 重複按下
- **WHEN** 請求進行中玩家再次按「開始冒險」
- **THEN** 按鈕為禁用，只有一個請求在進行

#### Scenario: 中斷也恢復
- **WHEN** 請求逾時或被中止
- **THEN** 按鈕與載入動畫在 `finally` 恢復，已填內容不遺失

### Requirement: 創角內容不寫入瀏覽器儲存
精靈 SHALL 只在頁面的記憶體中保存玩家填寫的角色卡、自我介紹與錨定事實，SHALL NOT 寫入 `localStorage`、`sessionStorage`、IndexedDB 或 cookie，SHALL NOT 放進網址。重新整理頁面 SHALL 回到序章。前端 SHALL NOT 在主控台或任何日誌輸出這些內容。

#### Scenario: 不寫入儲存
- **WHEN** 玩家填寫角色卡與自我介紹後觀察瀏覽器儲存
- **THEN** 沒有任何儲存區含有這些內容

#### Scenario: 重新整理
- **WHEN** 玩家在創角步驟重新整理頁面
- **THEN** 回到序章，先前填寫的內容不存在
