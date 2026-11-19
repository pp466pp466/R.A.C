# Spec Delta

## MODIFIED Requirements

### Requirement: 精靈頁的外殼與創角資料載入
`/game/start/<版本識別碼>` SHALL 由 MVC Page Controller 渲染，需要登入，初始狀態 SHALL 只含閘門結果、會員等級與請求逾時設定（由設定算出，前端不寫死），不含劇本內容。前端 SHALL 以 `GET /api/sessions/setup/<id>` 載入序章與創角資料；404 SHALL 顯示「找不到這個劇本」並提供回 `/game` 的連結。劇本的內容模式 SHALL NOT 造成載入失敗，精靈頁 SHALL NOT 有「內容模式高於目前模式」的說明。載入期間 SHALL 顯示載入中，失敗 SHALL 顯示重試。

#### Scenario: 載入成功
- **WHEN** 玩家開啟上架劇本的精靈頁
- **THEN** 顯示序章（背景故事）與步驟指示

#### Scenario: 找不到
- **WHEN** 玩家開啟不存在或別人草稿的版本
- **THEN** 顯示「找不到這個劇本」與回 `/game` 的連結，沒有顯示任何劇本內容

#### Scenario: 分級高於玩家模式
- **WHEN** 安全模式開的玩家開啟未綁定安全模式的劇本
- **THEN** 正常顯示序章與創角，沒有「內容模式高於目前模式」的說明，也沒有被拒絕

#### Scenario: 未登入
- **WHEN** 沒有登入就開啟精靈頁
- **THEN** 導向登入頁並帶回到該頁的返回位置

### Requirement: 開始冒險
創角步驟的「開始冒險」SHALL 以 `POST /api/sessions` 送出版本 `id`、角色卡（含來歷）與錨定事實或 `skip_anchor`；請求逾時 SHALL 取自頁面初始狀態（涵蓋兩層審查的最壞時間），並於離開頁面時以 AbortController 中止。送出期間 SHALL 禁用所有按鈕並顯示處理中，無論成功、失敗或中斷 SHALL 在 `finally` 恢復。成功（201）SHALL 導向 `/game/<新存檔的識別碼>`。失敗 SHALL 依代碼處理：400 把欄位錯誤顯示在對應欄位並跳回該步驟；422 `protagonist_rejected` 與 `script_rejected` 只顯示類別；503 `review_unavailable` 顯示請稍後再試；403 `age_confirmation_required` 與其他閘門代碼顯示對應說明（不再有 `content_tier_restricted`）；所有失敗 SHALL 保留玩家已填的內容。同一時間 SHALL 只送出一個開存檔請求。

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
