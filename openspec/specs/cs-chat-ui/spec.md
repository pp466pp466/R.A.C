# cs-chat-ui Specification

## Purpose
規定會員使用客服的瀏覽器介面：類 ChatGPT 的對話頁、可開關的歷史對話 Sidebar、打字機與快速回覆 chips，以及離線可閱讀、重送不重複的本機暫存與同步，並維持離線前端與 Vue 全面接管顯示狀態的架構規則。

## Requirements

### Requirement: 客服頁面需要登入
`/cs` SHALL 只回傳 HTML 外殼與初始狀態。未登入者 SHALL 被導向登入頁並在登入後回到 `/cs`。頁面 SHALL 顯示使用者的會員等級標籤；等級只影響額度，不影響畫面上可用的功能。

#### Scenario: 未登入
- **WHEN** 未登入者開啟 `/cs`
- **THEN** 被導向 `/login` 並保留 `next=/cs`

#### Scenario: 已登入
- **WHEN** 已登入的會員開啟 `/cs`
- **THEN** 看到對話區、Sidebar 與會員等級標籤

### Requirement: 被閘門擋下時引導使用者
客服 API 回應尚未完成年齡確認、信箱未驗證或服務暫時無法使用時，頁面 SHALL 以繁體中文說明原因；尚未完成年齡確認 SHALL 附上前往帳號設定補填的連結，且輸入區 SHALL 停用。

#### Scenario: 尚未填生日
- **WHEN** 沒有生日的帳號開啟 `/cs`
- **THEN** 顯示請先填寫生日的說明與前往帳號設定的連結，無法送出訊息

#### Scenario: 服務不可用
- **WHEN** API 回應 503
- **THEN** 顯示服務暫時無法使用的提示，已輸入的文字保留

### Requirement: Sidebar 的版面與狀態
左側 Sidebar 寬度 SHALL 為 280px，由 Vue 響應式變數以 `v-if` 或 `v-show` 完全接管，SHALL NOT 使用 Bootstrap 的 offcanvas 或任何 `data-bs-*` 互動。寬度 992px 以上 SHALL 預設開啟並推擠主區域；未滿 992px SHALL 預設關閉並以覆蓋方式顯示，附遮罩。

#### Scenario: 桌面預設
- **WHEN** 在寬度 1280px 首次開啟
- **THEN** Sidebar 開啟，主區域寬度縮減 280px

#### Scenario: 手機預設
- **WHEN** 在寬度 360px 開啟
- **THEN** Sidebar 關閉；開啟後覆蓋在主區域上並顯示遮罩

#### Scenario: 關閉覆蓋式 Sidebar
- **WHEN** 手機上點遮罩或按 Esc
- **THEN** Sidebar 關閉

### Requirement: Sidebar 的開關狀態只在桌面記住
桌面寬度的開關狀態 SHALL 記在 `localStorage`；讀寫 SHALL 包在例外處理內，失敗時退回預設值。手機與平板寬度 SHALL 不記住狀態，每次進入皆為關閉。選擇對話後，桌面維持開啟，手機與平板自動關閉。

#### Scenario: 桌面關閉後重新整理
- **WHEN** 桌面使用者關閉 Sidebar 後重新整理
- **THEN** Sidebar 維持關閉

#### Scenario: localStorage 不可用
- **WHEN** 瀏覽器禁止存取 `localStorage`
- **THEN** 頁面仍可使用，Sidebar 使用預設狀態

#### Scenario: 手機選擇對話
- **WHEN** 在手機寬度點選一個歷史對話
- **THEN** 載入該對話並自動關閉 Sidebar

### Requirement: 歷史對話的管理使用自製元件
Sidebar SHALL 列出自己的對話並可新增、改名與刪除。改名與刪除的確認 SHALL 使用自製的 Modal，SHALL NOT 使用瀏覽器內建對話框；結果提示 SHALL 使用自製 Toast。

#### Scenario: 刪除對話
- **WHEN** 使用者按下刪除
- **THEN** 出現自製確認 Modal，確認後對話從列表消失並顯示 Toast

#### Scenario: 改名衝突
- **WHEN** 改名時後端回應版本衝突
- **THEN** 列表改用後端的最新標題，並以 Toast 說明已更新

### Requirement: 訊息只以純文字顯示
聊天視窗 SHALL 只顯示回覆文字，意圖與實體 SHALL NOT 顯示。所有使用者與助理的文字 SHALL 以純文字渲染並保留換行，SHALL NOT 使用 `v-html` 或任何會解析標記語言的方式。降級文案 SHALL 有可辨識但不嚇人的標示。

#### Scenario: 回覆含 HTML
- **WHEN** 助理回覆含有 `<script>` 或 `<b>` 字樣
- **THEN** 畫面上逐字顯示這些字元，不被執行也不被當成標籤

#### Scenario: 降級訊息
- **WHEN** 歷史中有降級的助理訊息
- **THEN** 它帶有降級的視覺標示，與正常回覆區分

### Requirement: 新回覆以打字機效果顯示
新收到的助理回覆 SHALL 經打字機緩衝區逐字平滑顯示；重新載入的歷史訊息 SHALL 立即完整顯示。打字機進行中，使用者可立即顯示全部文字。

#### Scenario: 收到新回覆
- **WHEN** 後端回傳助理回覆
- **THEN** 文字逐字出現，結束後才顯示建議動作

#### Scenario: 載入歷史
- **WHEN** 切換到有 20 則訊息的對話
- **THEN** 所有訊息立即完整顯示，沒有打字機動畫

### Requirement: 建議動作以快速回覆 chips 呈現
建議動作 SHALL 顯示為最後一則助理訊息下方的快速回覆 chips，一次最多 4 個，打字機結束後淡入。樣式為膠囊形、1px 的 `#2A2D34` 邊框、透明背景、`#8A8F99` 文字，懸停時邊框與文字改為 `#FF6B35`。點擊 SHALL 以其文字作為使用者訊息送出，並以 `v-if` 移除該組 chips。

#### Scenario: 點擊 chip
- **WHEN** 使用者點擊一個建議動作
- **THEN** 該文字被當成使用者訊息送出，這組 chips 立即從畫面移除

#### Scenario: 之後已有使用者訊息
- **WHEN** 最後一則助理訊息之後已有新的使用者訊息
- **THEN** 不顯示任何 chips

#### Scenario: 重新整理
- **WHEN** 重新整理且最後一則是帶建議動作的助理訊息
- **THEN** chips 重新出現

### Requirement: 送出訊息的逾時與中止
送出訊息的請求逾時 SHALL 等於伺服器的單次請求總預算加 5 秒（預設 30 + 5 = 35 秒），數值由伺服器放進頁面的初始狀態，前端 SHALL NOT 自行寫死；並以 `AbortController` 在使用者取消、切換對話或離開頁面時中止。無論成功、失敗或中斷，SHALL 在 `finally` 恢復介面（解除送出鈕禁用、清除載入動畫）。前端中止不代表後端中止，結果 SHALL 在之後以序號同步補齊。

#### Scenario: 請求逾時
- **WHEN** 超過頁面初始狀態所給的逾時時間仍沒有回應
- **THEN** 介面恢復可輸入，訊息保留為待送並以相同識別碼重送

#### Scenario: 後端預算調整
- **WHEN** 伺服器的總預算被設定為 40 秒
- **THEN** 頁面收到的前端逾時為 45 秒，不需要修改任何前端檔案

#### Scenario: 切換對話
- **WHEN** 等待回覆時使用者切換到另一個對話
- **THEN** 請求被中止，介面不顯示載入中；回到原對話時以序號補齊後端已寫入的回覆

#### Scenario: 失敗後恢復
- **WHEN** 請求以任何錯誤結束
- **THEN** 送出鈕可再次使用，載入動畫消失

### Requirement: 本機暫存對話與待送訊息
前端 SHALL 以 IndexedDB 暫存對話與訊息，並保存尚未同步成功的待送訊息；連線正常時背景同步，衝突時以後端為準。離線時 SHALL 只能閱讀暫存的歷史並保存待送訊息，產生新回覆需要連線。

#### Scenario: 離線閱讀
- **WHEN** 沒有網路時開啟已暫存的對話
- **THEN** 顯示暫存的歷史，輸入的新訊息被標示為待送

#### Scenario: 連線恢復
- **WHEN** 網路恢復
- **THEN** 待送訊息依輸入順序、以原本的訊息識別碼逐一重送，不產生重複訊息

#### Scenario: 後端與暫存不一致
- **WHEN** 後端的訊息與本機暫存不同
- **THEN** 以後端為準，本機只保留尚未同步成功的待送紀錄

### Requirement: 暫存不可用時仍能使用
IndexedDB 無法使用（例如私密瀏覽模式）時，頁面 SHALL 退回僅記憶體的暫存並仍可對話，離線與重新整理後的待送訊息保存則不保證。

#### Scenario: IndexedDB 被禁止
- **WHEN** 開啟資料庫失敗
- **THEN** 頁面正常運作，介面不顯示錯誤對話框

### Requirement: 提示文字與版面
所有介面文字 SHALL 為繁體中文，頁面 SHALL 沿用全站的深色電影感色彩並符合離線前端規範（不使用外部資源）。寬度 360px 與 1280px 下 SHALL 沒有水平捲軸。

#### Scenario: 窄螢幕
- **WHEN** 在 360px 寬度開啟 `/cs`
- **THEN** 沒有水平捲軸，輸入區與送出鈕完整可見

#### Scenario: 外部資源
- **WHEN** 載入 `/cs`
- **THEN** 沒有任何對外部網域的請求

### Requirement: 前端區分用量相關的拒絕並不自動重送無法立即成功的訊息
前端 SHALL 依錯誤代碼處理用量拒絕：`rate_limited` 與 `system_busy` 為暫時性，訊息保留並隔一段時間自動重送；`daily_quota_exceeded`、`chat_suspended` 與 `conversation_limit_reached` 在短時間內不會成功，訊息 SHALL 標記為無法送出、不自動重送，並在訊息下方顯示伺服器回覆的繁體中文說明，讓使用者可以重試或移除。

#### Scenario: 每分鐘限制
- **WHEN** 送出訊息得到 429 `rate_limited`
- **THEN** 訊息保留為待送，稍後以同一個識別碼自動重送

#### Scenario: 今日額度用完
- **WHEN** 送出訊息得到 429 `daily_quota_exceeded`
- **THEN** 訊息標記為無法送出且不自動重送，訊息下方顯示額度已用完的說明

#### Scenario: 對話被暫停
- **WHEN** 送出訊息得到 403 `chat_suspended`
- **THEN** 訊息標記為無法送出，頁面顯示暫停與恢復時間的提示，且輸入區不再接受送出直到截止時間

### Requirement: Sidebar 顯示今日剩餘輪數
`/cs` 的 Sidebar 底部 SHALL 在會員等級標籤旁顯示今日剩餘的對話輪數，資料取自 `GET /api/cs/usage`，並在每則回覆之後更新。取得失敗或離線時 SHALL 隱藏這一行而不影響其他功能。

#### Scenario: 顯示剩餘
- **WHEN** free 會員今日已用 3 輪，開啟 `/cs`
- **THEN** Sidebar 顯示今日剩餘 27 輪

#### Scenario: 取得失敗
- **WHEN** 用量查詢失敗
- **THEN** 該行不顯示，對話與送出照常運作
