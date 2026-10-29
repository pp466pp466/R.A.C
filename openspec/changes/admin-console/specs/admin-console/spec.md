# Spec Delta

## Purpose

定義管理員後台的入口與總覽：誰可以進入 `/admin` 與 `/api/admin/*`、非管理員如何被擋下而不洩漏後台的存在，以及總覽頁能看到哪些統計——只有彙總，沒有任何個別使用者的資料。

## ADDED Requirements

### Requirement: 只有管理員可以使用後台
`/admin` 頁面與所有 `/api/admin/*` 端點 SHALL 只允許角色為 `admin` 且帳號為 `active` 的使用者存取。已登入但不是管理員者 SHALL 得到 404，且與路徑真的不存在時的回應相同；未登入者呼叫 API SHALL 得到 401 的 JSON 錯誤、開啟頁面 SHALL 導向登入頁並保留返回位置。會員等級 SHALL NOT 影響這個判斷。

#### Scenario: 一般使用者開啟後台頁面
- **WHEN** 已登入的一般使用者開啟 `/admin`
- **THEN** 回應 404，內容與不存在的頁面相同

#### Scenario: 一般使用者呼叫後台 API
- **WHEN** 已登入的一般使用者呼叫任一 `/api/admin/*` 端點
- **THEN** 回應 404 的 JSON 錯誤，與不存在的端點相同，沒有任何資料被讀取或寫入

#### Scenario: 未登入
- **WHEN** 未登入者呼叫 `/api/admin/overview`，或開啟 `/admin`
- **THEN** API 回應 401；頁面導向登入頁，登入成功後回到 `/admin`

#### Scenario: 高等級不是管理員
- **WHEN** 等級為 `vip` 的一般使用者開啟 `/admin`
- **THEN** 回應 404

#### Scenario: 停權的管理員
- **WHEN** 狀態為 `suspended` 的管理員使用既有的登入狀態呼叫後台
- **THEN** 登入狀態已失效，視為未登入

### Requirement: 後台的回應不可被快取
`/admin` 的 HTML 與所有 `/api/admin/*` 的回應（含錯誤）SHALL 帶有 `Cache-Control: no-store`。所有非 GET 的後台請求 SHALL 通過 CSRF 檢查，缺少權杖者 SHALL 得到 403 且不改變任何資料。

#### Scenario: 回應標頭
- **WHEN** 管理員取得 `/admin` 或呼叫任一後台 API
- **THEN** 回應含 `Cache-Control: no-store`

#### Scenario: 沒有 CSRF 權杖的寫入
- **WHEN** 管理員的登入狀態下，不帶 CSRF 權杖送出停權請求
- **THEN** 回應 403，使用者狀態與稽核紀錄都沒有改變

### Requirement: 總覽只有彙總資料
`GET /api/admin/overview` SHALL 回傳：各狀態與各會員等級的使用者人數、今日與近 7 日（以 Asia/Taipei 日界計）每日的 token 總數、模型呼叫次數、計入額度的對話輪數與被安全過濾擋下的呼叫次數、全站今日用量相對於 `USAGE_DAILY_TOKEN_LIMIT` 與 `USAGE_DAILY_CALL_LIMIT` 的使用比例，各年齡區間的人數，以及「今日警告」的三個計數：今日被內容安全過濾擋下的對話呼叫數、今日被擋過的使用者數、目前客服對話因累計被擋而暫停中的使用者數。回應 SHALL NOT 含任何個別使用者的 email、姓名、識別碼、生日、年齡區間或對話內容。

#### Scenario: 總覽內容
- **WHEN** 管理員取得總覽
- **THEN** 回應含上述統計，近 7 日恰有 7 筆且依日期排序，沒有任何個別使用者的資料

#### Scenario: 用量比例
- **WHEN** 全站今日 token 用量為上限的 40%、呼叫次數為上限的 10%
- **THEN** 總覽的兩個比例分別反映 0.4 與 0.1

#### Scenario: 年齡只有彙總
- **WHEN** 管理員取得總覽
- **THEN** 年齡資料只有各區間的人數與尚未完成確認的人數，沒有生日

#### Scenario: 今日警告
- **WHEN** 今日有兩位使用者的對話呼叫被擋下（其中一位累計三次而被暫停）
- **THEN** 警告計數為：被擋的呼叫數等於實際次數、被擋的使用者數為 2、暫停中的使用者數為 1

### Requirement: 警告是找使用者的主要入口
後台 SHALL 以「警告」作為管理員找到使用者的主要方式：總覽的「今日警告」SHALL 連到被標記對話的清單，清單的每一筆 SHALL 能進入該對話與該使用者的詳情，由此決定是否刪除內容或停權。以信箱查詢使用者 SHALL 是次要入口，介面 SHALL 標明它供客服反應（例如使用者來信要求調整等級）時使用，且 SHALL NOT 提供瀏覽或搜尋全部使用者的方式。

#### Scenario: 從警告到處理
- **WHEN** 管理員從總覽的今日警告進入被標記清單，再選一筆
- **THEN** 能讀該對話、進入該使用者的詳情，並執行停權或刪除對話

#### Scenario: 信箱查詢的用途說明
- **WHEN** 管理員開啟使用者分頁
- **THEN** 以信箱查詢的區塊標明「供客服反應時使用」，沒有使用者清單

### Requirement: 後台頁面只是外殼
`/admin` SHALL 只回傳 HTML 外殼與必要的初始狀態（管理員的顯示名稱），所有資料 SHALL 經由 `/api/admin/*` 取得。頁面 SHALL 以純文字呈現所有來自使用者或模型的內容，SHALL NOT 使用 `v-html`，Modal 與 Toast 的顯示 SHALL 由 Vue 的響應式狀態接管，且 SHALL 沿用離線的前端資產與既定的色彩規範。

#### Scenario: 對話內容含標記
- **WHEN** 被檢視的對話內容含有 `<script>` 或 HTML 標籤
- **THEN** 頁面以純文字顯示，沒有被當成標記執行

#### Scenario: 離線資產
- **WHEN** 載入 `/admin`
- **THEN** 所有腳本、樣式與字型都來自本機的靜態資產，沒有外部網域的請求
