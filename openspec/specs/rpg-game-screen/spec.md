# rpg-game-screen Specification

## Purpose

定義 `/game/<存檔識別碼>` 遊戲主畫面的外殼、載入順序、劇情時間軸與行動輸入、世界狀態面板、已完結與內容模式提示，以及前端離線與狀態接管規範。串流接收、本機暫存、遊戲幣與記事簿各有自己的能力規格。

## Requirements

### Requirement: 遊戲頁的外殼與初始狀態
`/game/<存檔識別碼>` SHALL 由 MVC Page Controller 渲染，需要登入；非擁有者與不存在 SHALL 得到相同的 404 頁面，權限判斷 SHALL 在 Service。頁面 SHALL 以已跳脫的 JSON 區塊傳遞初始狀態：存檔識別碼、劇本標題、內容模式、是否已完結、主角姓名、會員等級、帳號閘門結果、串流逾時與行動字數上限；初始狀態 SHALL NOT 含回合內容、第 0 幕、記事簿、世界狀態或 email、生日與年齡。

#### Scenario: 自己的存檔
- **WHEN** 玩家開啟自己存檔的 `/game/<id>`
- **THEN** 回應 200 的 HTML，初始狀態含上述欄位，且沒有任何劇情或記事簿內容

#### Scenario: 別人的存檔
- **WHEN** 玩家 B 開啟玩家 A 存檔的 `/game/<id>`
- **THEN** 得到與不存在存檔逐字相同的 404 頁面

#### Scenario: 未登入與受限登入狀態
- **WHEN** 未登入者或被鎖定的玩家開啟 `/game/<id>`
- **THEN** 分別導向登入頁（帶回到該網址的返回位置）與 `/appeal`

#### Scenario: 開存檔的網址不被當成存檔
- **WHEN** 玩家開啟 `/game/start/<版本 id>`
- **THEN** 顯示開存檔精靈，而不是遊戲頁

### Requirement: 載入順序與第 0 幕
頁面掛載後 SHALL 先讀存檔快照，再依第 0 幕狀態處理：`ready` 或 `fallback` 直接顯示既有文字，SHALL NOT 重新生成；`pending` 或 `generating` 時 SHALL 自動呼叫開場串流並以打字機顯示。開場串流回 409 `opening_in_progress`（另一個裝置正在生成）時 SHALL 顯示等待說明並稍後重讀快照。第 0 幕完成之前，行動輸入 SHALL 禁用。第 0 幕不是一輪，SHALL NOT 計入輪數或消耗遊戲幣。

#### Scenario: 第 0 幕已完成
- **WHEN** 快照的第 0 幕為 `ready`
- **THEN** 直接顯示文字，沒有呼叫開場串流

#### Scenario: 第 0 幕尚未生成
- **WHEN** 快照的第 0 幕為 `pending`
- **THEN** 呼叫開場串流，文字逐字顯示，`done` 之後輸入解鎖

#### Scenario: 另一個裝置正在生成
- **WHEN** 開場串流回 409 `opening_in_progress`
- **THEN** 顯示「另一個裝置正在準備開場」的說明，稍後重讀快照而不顯示錯誤

#### Scenario: 開場失敗
- **WHEN** 開場串流以 `error` 結束
- **THEN** 顯示繁體中文錯誤與重試按鈕，輸入維持禁用

### Requirement: 劇情時間軸與歷史補齊
頁面 SHALL 以 `since_seq` 增量讀取回合，並依 `seq` 遞增排列；`has_more` 為真時 SHALL 繼續讀取。`complete` 的回合顯示玩家行動與劇情；被擋或中斷的回合 SHALL 只顯示一行轉場或中斷說明，SHALL NOT 顯示任何部分內容。劇情與行動 SHALL 以純文字渲染（保留換行），SHALL NOT 使用 `v-html`。新回合結束時 SHALL 捲動到最底，但玩家正在閱讀較早內容時 SHALL NOT 強制捲動。

#### Scenario: 載入歷史
- **WHEN** 存檔有 25 個完整回合
- **THEN** 時間軸依序顯示第 0 幕與 25 輪，序號遞增

#### Scenario: 被擋回合
- **WHEN** 歷史含一個被擋的回合
- **THEN** 該處只有一行轉場說明，沒有被擋的文字

#### Scenario: 劇情含標記字元
- **WHEN** 劇情或玩家行動含 `<b>` 或 `<script>` 字樣
- **THEN** 畫面顯示原文字元，沒有被解析為 HTML

#### Scenario: 閱讀較早內容
- **WHEN** 玩家把時間軸捲離最底，這時新的片段到達
- **THEN** 畫面不被拉到最底，並提供「回到最新」的按鈕

### Requirement: 行動輸入與送出的互動
行動輸入框 SHALL 在第 0 幕完成、存檔未完結、閘門為空、沒有進行中的回合時才可送出；空白或超過上限的行動 SHALL 在前端擋下並說明，不送出請求。送出時 SHALL 以新的 `client_msg_id` 呼叫回合串流；進行中 SHALL 禁用送出並顯示可取消的載入狀態，取消 SHALL 中止請求；`done` 之後或任何失敗 SHALL 在 `finally` 恢復輸入並保留玩家尚未送出成功的文字。

#### Scenario: 一般送出
- **WHEN** 玩家輸入行動並送出
- **THEN** 時間軸出現玩家行動，AI 回應逐字顯示，`done` 之後輸入解鎖並清空

#### Scenario: 空白行動
- **WHEN** 玩家只輸入空白後送出
- **THEN** 不送出請求，欄位旁說明行動不可為空

#### Scenario: 失敗時保留文字
- **WHEN** 回合串流以 `error` 結束
- **THEN** 輸入框保留玩家的行動文字，按鈕恢復可用，顯示繁體中文錯誤

#### Scenario: 進行中不可重複送出
- **WHEN** 回合進行中玩家再次按送出
- **THEN** 沒有第二個請求

### Requirement: 世界狀態面板
頁面 SHALL 顯示世界狀態的玩家視圖：目前地點、經過月數、主角目前的年齡、進行中的目標與去過的地點；每次 `done` 後 SHALL 依 `state` 事件或重讀快照更新。面板 SHALL NOT 顯示 NPC 的特質、隱藏的歸屬或弧線階段。`state` 事件含年齡更正等通知時 SHALL 以 Toast 或時間軸內一行說明告知玩家（含更正前的值）。側欄（世界狀態與記事簿的面板）SHALL 在桌面與手機都預設收合，頂列 SHALL 有開關按鈕（含可及性標籤與目前狀態）；桌面（寬度 992px 以上）開啟時推擠主區域，手機與平板（未滿 992px）以覆蓋方式開啟並附遮罩；開關狀態由 Vue 響應式變數接管，SHALL NOT 記住（不寫入任何瀏覽器儲存）。

#### Scenario: 回合結束更新
- **WHEN** 一輪結束且地點改變
- **THEN** 面板顯示新的地點與經過月數

#### Scenario: 年齡更正通知
- **WHEN** `state` 事件帶有主角或 NPC 的年齡更正通知
- **THEN** 玩家看到一則說明，含更正前的年齡

#### Scenario: 預設收合與開關
- **WHEN** 玩家在桌面寬度開啟遊戲頁
- **THEN** 側欄為收合，時間軸佔滿主區域；按頂列的開關按鈕後側欄開啟並推擠主區域，再按一次收合

#### Scenario: 手機的覆蓋側欄
- **WHEN** 玩家在 360px 寬度按開關按鈕
- **THEN** 側欄以覆蓋方式開啟並附遮罩，按遮罩或關閉按鈕即收合

#### Scenario: 不記住開關
- **WHEN** 玩家開啟側欄後重新整理頁面
- **THEN** 側欄回到收合，且沒有任何瀏覽器儲存被寫入

#### Scenario: 不洩漏隱藏資訊
- **WHEN** 檢查面板與時間軸的所有顯示欄位
- **THEN** 沒有 NPC 的隱藏歸屬、特質或弧線階段

### Requirement: 內容模式、年齡限制與完結的提示
頁面 SHALL 顯示存檔的內容模式；玩家為未成年而固定安全模式時 SHALL 以一句話說明原因。帳號閘門非空時 SHALL 禁用輸入並顯示對應的繁體中文提示與前往帳號設定的連結，歷史仍可閱讀。存檔已完結 SHALL 顯示「已完結」，禁用輸入與記事簿編輯，歷史與世界狀態仍可閱讀，並提供回到 `/game` 以同一劇本開新存檔的連結。頁面 SHALL NOT 自動調高任何模式。

#### Scenario: 已完結
- **WHEN** 玩家開啟已完結的存檔
- **THEN** 歷史可讀，輸入框禁用並說明已完結，有回到 `/game` 的連結

#### Scenario: 閘門禁用輸入
- **WHEN** 閘門為 `age_confirmation_required` 的管理員開啟存檔
- **THEN** 歷史可讀、輸入禁用，並附前往帳號設定的連結

#### Scenario: 未成年玩家
- **WHEN** 13 至 17 歲玩家開啟存檔
- **THEN** 顯示安全模式與其原因

### Requirement: 遊戲頁遵守前端離線與狀態接管規範
遊戲頁與其腳本 SHALL 使用 Vue 3 全域變數模式（不使用 `.vue` 與打包工具）；一般請求用離線的 axios，串流用原生 `fetch`；資源 SHALL 全部來自 `static/vendor/` 與 `static/fonts/`，不使用 CDN。Modal、Toast、面板與 Sidebar 的顯示狀態 SHALL 由 Vue 響應式變數接管，SHALL NOT 使用 Bootstrap 的 JavaScript 外掛或 `data-bs-*`（深色模式屬性除外）。版面 SHALL 在 360px 寬度下不橫向溢出，且不使用純白 `#FFFFFF`。日誌與主控台 SHALL NOT 輸出玩家行動、劇情、記事簿或角色卡。

#### Scenario: 架構守門
- **WHEN** 執行前端規則的架構測試
- **THEN** 新增的樣板與腳本沒有 CDN、`v-html`、Bootstrap JavaScript 與 `data-bs-*`（深色模式除外）

#### Scenario: 手機寬度
- **WHEN** 以 360px 寬度開啟遊戲頁
- **THEN** 沒有橫向捲軸，輸入區固定在底部且不遮住最後一則劇情

#### Scenario: 沒有主控台輸出
- **WHEN** 以沙盒執行完整的載入與一輪流程並攔截 `console`
- **THEN** 沒有任何玩家文字被輸出
