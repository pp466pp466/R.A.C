# rpg-script-catalog Specification

## Purpose
定義 RPG 的完整公開劇本目錄：關鍵字搜尋、標籤與讀者頻道篩選、游標分頁、常用標籤、詳情端點與詳情頁的欄位白名單，以及入口 `/game` 的搜尋與篩選畫面。可見範圍與故事偏好排序沿用 `rpg-entry-api` 與 `story-preference`。

## Requirements

### Requirement: 關鍵字只比對一般欄位
`GET /api/sessions/catalog?q=` SHALL 以關鍵字篩選上架版本：關鍵字經 NFC 正規化、去除控制字元、不分大小寫，以空白分成至多 5 個詞（整體至多 50 字），每個詞 SHALL 同時符合（AND）；每個詞只比對標題、簡介、世界名稱與標籤，SHALL NOT 比對背景、角色、里程碑等 JSON 或長文字欄位。比對 SHALL 把 `%`、`_` 與跳脫字元當一般字元。空字串視為沒有關鍵字；超過詞數或長度 SHALL 回 400 並指出 `q`。大小寫行為 SHALL 在 SQLite 與 SQL Server 一致。

#### Scenario: 比對標題與標籤
- **WHEN** 一份劇本標題含「青雲」、另一份標籤為「仙俠」，分別以 `q=青雲` 與 `q=仙俠` 搜尋
- **THEN** 各自只得到對應的劇本

#### Scenario: 不比對內文
- **WHEN** 關鍵字只出現在某劇本的背景與角色文字
- **THEN** 該劇本不在結果內

#### Scenario: 多個詞都要符合
- **WHEN** `q=青雲 仙俠`，一份同時符合、一份只符合其中一個詞
- **THEN** 只有同時符合者在結果內

#### Scenario: 大小寫
- **WHEN** 標籤為 `SciFi`，以 `q=scifi` 搜尋
- **THEN** 該劇本在結果內，SQLite 與 SQL Server 結果相同

#### Scenario: 萬用字元視為一般字元
- **WHEN** `q=%`，沒有任何標題含 `%`
- **THEN** 結果為空，不是全部

#### Scenario: 過多的詞
- **WHEN** `q` 有 6 個詞
- **THEN** 回應 400 並指出 `q`

### Requirement: 標籤與讀者頻道篩選
目錄 SHALL 接受 `tag`（單一標籤，不分大小寫的完整比對）與 `audience`（`all`、`male`、`female`）。`audience` 為 `male` 或 `female` 時只含該頻道的版本，為 `all` 時只含全讀者（含舊的空值）的版本，未給時不篩選。三種條件（`q`、`tag`、`audience`）SHALL 以 AND 組合，並 SHALL 與故事偏好排序、「只含上架版本」的規則並存；不合法的 `tag` 長度或 `audience` 值 SHALL 回 400 並指出欄位。

#### Scenario: 依標籤
- **WHEN** `tag=仙俠`
- **THEN** 只有標籤含「仙俠」的上架版本

#### Scenario: 依頻道
- **WHEN** `audience=female`，有男頻、女頻、全讀者與空值頻道各一份
- **THEN** 只有女頻

#### Scenario: 全讀者含舊值
- **WHEN** `audience=all`
- **THEN** 結果含頻道為 `all` 與空值的版本

#### Scenario: 條件組合
- **WHEN** `q`、`tag`、`audience` 同時給
- **THEN** 結果同時滿足三者

#### Scenario: 不合法的頻道
- **WHEN** `audience=other`
- **THEN** 回應 400 並指出 `audience`

### Requirement: 游標分頁取代位移
目錄 SHALL 以不透明的 `cursor` 分頁：回應含 `has_more` 與 `next_cursor`（沒有更多時為空），請求帶上一頁的 `next_cursor` 取得下一頁；`limit` 預設 12、至多 24。排序 SHALL 為偏好層、發佈時間由新到舊、編號由新到舊，游標位置 SHALL 以這三個鍵決定，使分頁之間在新劇本上架、舊劇本下架或恢復時不重複也不遺漏已讀過之後的項目。`offset` 參數 SHALL 不再被讀取。損毀或被竄改的 `cursor` SHALL 回 400 並指出 `cursor`，不得造成 500。

#### Scenario: 串起來等於一次取得
- **WHEN** 以 `limit=2` 沿著 `next_cursor` 讀完全部
- **THEN** 串起來的順序與一次取得全部相同，沒有重複或遺漏

#### Scenario: 分頁期間有新劇本上架
- **WHEN** 讀完第一頁後有一份新劇本上架，再讀第二頁
- **THEN** 第二頁不含第一頁已出現的項目

#### Scenario: 分頁期間有劇本下架
- **WHEN** 讀完第一頁後，第一頁中的一份劇本被下架，再讀第二頁
- **THEN** 第二頁的內容與順序不受影響

#### Scenario: 偏好分層下的游標
- **WHEN** 偏好為男頻，以 `limit=1` 讀完男頻、全讀者、女頻各一份
- **THEN** 順序為男頻、全讀者、女頻，每份只出現一次

#### Scenario: 損毀的游標
- **WHEN** `cursor` 為任意字串或結構不合的內容
- **THEN** 回應 400 並指出 `cursor`

#### Scenario: 位移不再使用
- **WHEN** 請求帶 `offset=5`
- **THEN** 該參數被忽略，結果與沒有帶時相同

### Requirement: 常用標籤
`GET /api/sessions/catalog/tags` SHALL 回傳上架版本中最常用的標籤（至多 30 個，依使用數由多到少、同數依標籤排序），每筆含標籤（小寫）與使用該標籤的上架版本數；已下架、待審、草稿與被取代的版本 SHALL NOT 計入。存取規則同目錄（登入即可，不過年齡閘門）。

#### Scenario: 計數只含上架
- **WHEN** 標籤「仙俠」在兩個上架版本與一個已下架版本上
- **THEN** 回傳的數量為 2

#### Scenario: 不分大小寫合併
- **WHEN** `SciFi` 與 `scifi` 各在一個上架版本上
- **THEN** 合併成一筆 `scifi`，數量 2

#### Scenario: 上限
- **WHEN** 上架版本共有 40 種標籤
- **THEN** 只回傳 30 筆

### Requirement: 劇本詳情只含公開的白名單欄位
`GET /api/sessions/catalog/<版本識別碼>` SHALL 只對 `published` 的版本回傳詳情，其他狀態（含擁有者自己的非上架版本）與不存在的識別碼 SHALL 得到相同的 404。回應 SHALL 只含：`id`、標題、簡介、世界名稱、標籤、筆名與短代號、內容模式標示、讀者頻道、版本編號、發佈時間、背景與體系、角色的姓名與職業、地點、是否有自訂結局，以及與目錄相同的 `content_mode`。SHALL NOT 含：角色的隱藏特質與個性、里程碑、結局說明文字、規則文字、初始對話、審查結果、內容雜湊、作者的使用者編號與任何內部整數編號。

#### Scenario: 詳情內容
- **WHEN** 玩家讀取上架版本的詳情
- **THEN** 有背景、角色姓名與職業、地點與標籤，沒有隱藏特質、里程碑與結局說明

#### Scenario: 已下架
- **WHEN** 玩家（含作者）讀取已下架版本的詳情
- **THEN** 回應 404

#### Scenario: 他人的草稿
- **WHEN** 玩家以他人草稿的識別碼讀取詳情
- **THEN** 回應 404，與不存在相同

#### Scenario: 不過年齡閘門
- **WHEN** 尚未完成年齡確認的管理員讀取詳情
- **THEN** 可以讀取（開存檔仍由既有閘門擋下）

### Requirement: 詳情頁
`GET /game/script/<版本識別碼>` SHALL 渲染詳情頁外殼（`login_required_page`，受限狀態導向既有頁面），初始狀態只含會員等級與帳號閘門，不含劇本內容、也不使用版本識別碼；資料由前端呼叫詳情端點取得，404 時顯示「找不到這個劇本」。頁面 SHALL 顯示詳情欄位（純文字渲染，嚴禁 `v-html`），並有「開始冒險」按鈕導向 `/game/start/<id>`、返回目錄的連結；安全模式開的玩家對未綁定安全模式的劇本按下「開始冒險」時 SHALL 先詢問是否修改安全模式（與入口頁同一個 Modal 與同一套行為）。閘門非空時按鈕停用。

#### Scenario: 開始冒險
- **WHEN** 已關閉安全模式的玩家在詳情頁按「開始冒險」
- **THEN** 導向 `/game/start/<該版本的 id>`

#### Scenario: 安全模式詢問
- **WHEN** 安全模式開的玩家在未綁定劇本的詳情頁按「開始冒險」
- **THEN** 先出現詢問 Modal，選擇維持安全模式才繼續到精靈

#### Scenario: 找不到
- **WHEN** 開啟已下架版本的詳情頁
- **THEN** 顯示「找不到這個劇本」與返回目錄的連結

#### Scenario: 作者文字不被當標記
- **WHEN** 劇本背景含 `<script>` 字樣
- **THEN** 頁面顯示原文，沒有被執行

### Requirement: 目錄畫面的搜尋與篩選
入口頁「選劇本」分頁 SHALL 有關鍵字搜尋框（按 Enter 或按鈕才送出）、常用標籤晶片（點選套用、再點取消）、讀者頻道選單（全部、男頻、女頻、全讀者）與「清除篩選」；卡片上的標籤可點選以套用標籤篩選，卡片標題可點進詳情頁。任何條件改變 SHALL 清空目前清單並從第一頁重新載入；「載入更多」沿用 `next_cursor`。搜尋無結果 SHALL 顯示說明與「清除篩選」，載入失敗 SHALL 顯示重試且不清除已載入內容；快速連續改變條件時，過時的回應 SHALL NOT 蓋掉較新的結果。所有顯示由 Vue 的響應式狀態接管，文字以純文字渲染。

#### Scenario: 套用標籤
- **WHEN** 玩家點選標籤晶片「仙俠」
- **THEN** 清單重新載入，只含該標籤的劇本，晶片呈選取狀態

#### Scenario: 無結果
- **WHEN** 關鍵字沒有任何符合
- **THEN** 顯示「沒有符合的劇本」與「清除篩選」按鈕，不顯示錯誤

#### Scenario: 過時的回應
- **WHEN** 玩家連續送出兩次搜尋，第一次的回應較晚抵達
- **THEN** 畫面只顯示第二次的結果

#### Scenario: 載入更多沿用條件
- **WHEN** 篩選條件下的清單按「載入更多」
- **THEN** 請求帶同樣的條件與 `next_cursor`

#### Scenario: 不使用瀏覽器儲存
- **WHEN** 玩家使用搜尋與篩選
- **THEN** 不寫入任何瀏覽器儲存
