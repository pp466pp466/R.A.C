# Spec Delta

## Purpose

定義 RPG 入口的頁面外殼與導覽：`/game` 入口頁（選劇本與我的存檔）、`/game/<存檔識別碼>` 的空殼頁，以及各頁之間的連結。頁面只渲染外殼與初始狀態，資料由唯讀端點提供；遊戲主畫面屬後續的 change。

## ADDED Requirements

### Requirement: 入口頁的外殼與初始狀態
`/game` SHALL 由 MVC Page Controller 渲染，需要登入；受限登入狀態 SHALL 由既有的登入檢查導向各自的頁面。頁面 SHALL 以 JSON 區塊（已跳脫，不以字串拼進腳本）傳遞初始狀態：會員等級、故事偏好與帳號閘門的結果（`null` 或 `age_confirmation_required`、`age_restricted`、`forbidden`，完全來自 AccessPolicy）。清單內容 SHALL NOT 放進初始狀態，由前端呼叫兩個唯讀端點取得。HTML SHALL 標註 `lang="zh-Hant-TW"` 與 UTF-8，所有資源 SHALL 來自本機並以 `url_for` 引入。

#### Scenario: 已登入的玩家
- **WHEN** 已完成年齡確認的玩家開啟 `/game`
- **THEN** 回應 200 的 HTML，初始狀態含會員等級、故事偏好與空的閘門結果，且沒有劇本或存檔內容

#### Scenario: 未登入
- **WHEN** 沒有登入就開啟 `/game`
- **THEN** 導向登入頁並帶回到 `/game` 的返回位置

#### Scenario: 受限登入狀態
- **WHEN** 被鎖定的玩家開啟 `/game`
- **THEN** 導向 `/appeal`

#### Scenario: 管理員尚未完成年齡確認
- **WHEN** 尚未完成年齡確認的管理員開啟 `/game`
- **THEN** 初始狀態的閘門為 `age_confirmation_required`，頁面顯示前往帳號設定補填的提示

### Requirement: 選劇本與我的存檔兩個分頁
入口頁 SHALL 有「選劇本」與「我的存檔」兩個分頁，預設為「選劇本」；分頁的顯示由 Vue 的響應式變數接管。「選劇本」SHALL 以卡片列出可玩劇本（標題、簡介、標籤、筆名與短代號、內容模式與讀者頻道），並以「載入更多」依 `has_more` 取得下一頁；按下卡片的「開始」SHALL 導向 `/game/start/<id>`。「我的存檔」SHALL 列出存檔（標題、主角姓名、已完成輪數、是否已完結、最後活動時間），按下 SHALL 導向 `/game/<存檔識別碼>`。載入中、空清單（附引導文字）與載入失敗（附重試）SHALL 各有明確的畫面。所有來自玩家與作者的文字 SHALL 以純文字渲染，嚴禁 `v-html`。

#### Scenario: 預設分頁
- **WHEN** 玩家開啟 `/game`
- **THEN** 顯示「選劇本」分頁並載入第一頁卡片

#### Scenario: 載入更多
- **WHEN** 清單的 `has_more` 為真，玩家按「載入更多」
- **THEN** 下一頁卡片接在後面，按鈕在載入期間禁用並於完成後恢復

#### Scenario: 開始一個劇本
- **WHEN** 玩家在卡片上按「開始」
- **THEN** 導向 `/game/start/<該版本的 id>`

#### Scenario: 繼續一個存檔
- **WHEN** 玩家在「我的存檔」按一個存檔
- **THEN** 導向 `/game/<該存檔的識別碼>`

#### Scenario: 空清單
- **WHEN** 玩家沒有任何存檔
- **THEN** 「我的存檔」顯示說明與回到「選劇本」的按鈕，不顯示錯誤

#### Scenario: 載入失敗
- **WHEN** 清單請求失敗
- **THEN** 顯示繁體中文的錯誤說明與重試按鈕，已載入的卡片不消失

#### Scenario: 作者的文字不被當成標記
- **WHEN** 劇本的標題含 `<b>` 之類的標記字元
- **THEN** 畫面顯示那些字元的原文，沒有被解析為 HTML

### Requirement: 內容模式與帳號閘門的提示
入口頁 SHALL 顯示玩家目前生效的內容模式；`is_adult` 為假時 SHALL 以一句話說明未滿 18 歲固定為安全模式，所以部分劇本不顯示；成年玩家的模式較保守時 SHALL 提示可到帳號設定調整，且頁面 SHALL NOT 自動調高任何模式。帳號閘門非空時 SHALL 顯示對應的繁體中文提示與前往帳號設定的連結，並禁用「開始」按鈕；清單仍可瀏覽。

#### Scenario: 未成年玩家
- **WHEN** 13 至 17 歲的玩家開啟 `/game`
- **THEN** 顯示安全模式與其原因，部分劇本因分級不顯示的說明可見

#### Scenario: 成年玩家的安全模式
- **WHEN** 成年但自行選擇安全模式的玩家開啟 `/game`
- **THEN** 提示可到帳號設定調整內容模式，頁面沒有任何改變模式的動作

#### Scenario: 閘門禁用開始
- **WHEN** 閘門為 `age_confirmation_required` 的管理員開啟 `/game`
- **THEN** 清單可瀏覽，「開始」按鈕為禁用並附前往帳號設定的連結

### Requirement: 存檔頁的空殼
`/game/<存檔識別碼>` SHALL 由 MVC Page Controller 渲染，需要登入；存檔 SHALL 由呼叫者擁有，非擁有者與不存在 SHALL 得到相同的 404 頁面。頁面 SHALL 顯示劇本標題、主角姓名、存檔的內容模式與是否已完結，並說明劇情畫面建置中，提供回到 `/game` 的連結。頁面 SHALL NOT 顯示任何回合內容、記事簿或世界狀態，也 SHALL NOT 呼叫任何會產生內容或扣遊戲幣的端點。路徑 `/game/start/<id>` 與 `/game/<存檔識別碼>` SHALL 互不衝突。

#### Scenario: 自己的存檔
- **WHEN** 玩家開啟自己存檔的 `/game/<id>`
- **THEN** 回應 200，顯示標題、主角姓名與「劇情畫面建置中」，並有回到 `/game` 的連結

#### Scenario: 別人的存檔
- **WHEN** 玩家 B 開啟玩家 A 存檔的 `/game/<id>`
- **THEN** 得到與不存在存檔相同的 404 頁面

#### Scenario: 不呼叫產生內容的端點
- **WHEN** 開啟空殼頁
- **THEN** 頁面沒有送出回合、第 0 幕或記事簿的請求，遊戲幣餘額不變

#### Scenario: 開存檔的網址不被當成存檔
- **WHEN** 玩家開啟 `/game/start/<版本 id>`
- **THEN** 顯示開存檔精靈，而不是 404 的存檔頁

### Requirement: 各頁導覽互通
`/game`、`/scripts`、`/account` 與 `/cs` SHALL 各有連到其他入口的導覽連結：`/game` 有「客服」「創作」「帳號」，其餘頁面有「冒險」連回 `/game`。連結 SHALL 為一般的站內連結，SHALL NOT 使用 Bootstrap 的 JavaScript 或 `data-bs-*` 互動屬性。

#### Scenario: 由客服回到冒險
- **WHEN** 玩家在 `/cs` 按「冒險」
- **THEN** 前往 `/game`

#### Scenario: 由入口到創作
- **WHEN** 玩家在 `/game` 按「創作」
- **THEN** 前往 `/scripts`

### Requirement: 入口頁遵守前端離線與狀態接管規範
入口頁、存檔頁與其腳本 SHALL 使用 Vue 3 全域變數模式（不使用 `.vue` 與打包工具），一般請求用離線的 axios，資源 SHALL 全部來自 `static/vendor/` 與 `static/fonts/`，不使用 CDN。Modal、Toast 與分頁的顯示狀態 SHALL 由 Vue 響應式變數接管，SHALL NOT 使用 Bootstrap 的 JavaScript 外掛或 `data-bs-*`（深色模式屬性除外）。版面 SHALL 在 360px 寬度下不橫向溢出。

#### Scenario: 架構守門
- **WHEN** 執行前端規則的架構測試
- **THEN** 新增的樣板與腳本沒有 CDN、`v-html`、Bootstrap JavaScript 與 `data-bs-*`（深色模式除外）

#### Scenario: 手機寬度
- **WHEN** 以 360px 寬度開啟 `/game`
- **THEN** 頁面沒有橫向捲軸，卡片單欄排列
