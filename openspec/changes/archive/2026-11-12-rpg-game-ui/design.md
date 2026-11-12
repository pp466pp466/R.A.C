# Design

## Context

動機與範圍見 `proposal.md`；行為契約見 `specs/`。這份只講怎麼做與為什麼。

現況（已讀過程式）：

- **後端已齊，本 change 不改任何端點**：`POST /api/game/sessions/<id>/turns/stream`、`…/opening/stream`、`GET /api/game/sessions/<id>`（快照：第 0 幕、世界狀態玩家視圖、已完成輪數、記事簿摘要）、`GET …/turns?since_seq=&limit=`（`complete` 才含劇情）、`GET`／`PUT …/notebook`、`GET /api/wallet`、`POST /api/wallet/checkin`。串流的開始前失敗（驗證、閘門、`insufficient_coins`、`turn_in_progress`、`opening_in_progress`、404、沒有供應商 503）都是一般 JSON 錯誤；開始後的失敗是 `error` 事件。事件為 `meta`／`token`／`ping`／`transition`／`state`／`done`／`error`，`meta.turn_id` 就是回合的 `seq`。
- **`/game/<存檔>` 現在是伺服器渲染的空殼**（`session.html`，沒有 Vue、沒有腳本）；`GameEntryService.session_summary` 提供標題、主角姓名、內容模式、是否完結，非擁有者與不存在同為 `NotFound`。
- **可重用的前端**：`UI.TypewriterBuffer`（`push`／`finish`／`play`，計時器可注入）、`UI.Modal`／`UI.toast`／`UI.install`、`Auth.readInitialState`／`Auth.describeError`（axios 錯誤整理）／`Auth.loginUrlFor`、`game-labels.js`（列舉對照）、`game-api.js`（axios 封裝）。客服的 `cs-store.js`（IndexedDB＋記憶體退路）與 `cs-sync.js` 是本機暫存的樣板。
- **串流計時的既有決定**：`docs/RPG.md` 7.15、7.17.3——前端閒置逾時 45 秒（任何位元組重設）、總逾時 180 秒；最壞時間（被擋兩次加狀態呼叫）約 150 秒。
- **驗收工具已有 RPG 假模型**：`scripts/rpg_acceptance_harness.py` 的 `FakeRpgModel` 能依行動文字的關鍵字產生劇情、被擋一次／兩次、中斷、慢速、結局與記事簿超量，專案外的 `newprojet-acceptance` 繼承它。所以瀏覽器實測可以走完整的一輪（RC-3a 的 AI 生成預覽才是缺的那一塊）。

## Goals / Non-Goals

**Goals：**

- 玩家按「開始冒險」後直接進入能玩的畫面：看到第 0 幕、輸入行動、看到串流與打字機、餘額被扣、世界狀態更新、能看與改記事簿。
- 串流的接收與狀態機寫成可在 Node 測試的純邏輯（SSE 解析、事件處理、錯誤對照），掛載層只做接線。
- 後端為權威：前端任何時候都能由後端重建；本機暫存壞掉或過期不會造成錯誤的畫面。
- 玩家文字（行動、劇情、記事簿）不進主控台、網址、日誌，也不被當成 HTML。

**Non-Goals：**

- 玩家重新生成與編輯輸入的按鈕（RC-2c）；錢包記錄頁、儲值與訂閱（W2）；完整目錄（RC-8）；刪除或封存存檔；自動排隊重送；圖片。
- 任何後端行為、資料表、遷移、環境變數、日誌事件的變動。
- 在瀏覽器以外的環境（Service Worker、離線 PWA）。

## Decisions

### D1 網址與初始狀態：沿用 `/game/<存檔識別碼>`，`session.html` 整頁取代

控制器 `game_session_page` 仍呼叫 `GameEntryService.session_summary`（擁有者判斷與 404 全在 Service），改為渲染遊戲畫面，初始狀態（`tojson` 已跳脫的 JSON 區塊）只放：`session`（`id`、`title`、`content_tier`、`ended`、`player_name`）、`user`（`public_id`、`member_tier`）、`gate`（`AccessPolicy.chat_gate`）、`stream`（`idle_timeout_ms`＝45000、`total_timeout_ms`＝180000）、`limits`（`action_max`，取自 `game_service.ACTION_MAX`，目前 1000）。逾時是控制器模組內的具名常數，由測試固定數值；字數上限直接引用後端常數，後端調整時前端一起變（前端不寫死，與 RC-3a 的精靈同一個做法）。快照、第 0 幕、回合、記事簿、世界狀態**不**放進初始狀態，一律由前端呼叫既有端點取得——頁面外殼因此永遠不含玩家文字以外的劇情內容，也不必改 Service。

- 備選：把快照也塞進初始狀態省一次請求。拒絕：快照會隨回合改變，頁面快取或返回上一頁會看到舊內容；多一條「初始狀態與端點不一致」的路徑要維護。
- `user.public_id` 用於 IndexedDB 名稱（客服 `chat.js` 已這樣做，是對外識別碼，非內部編號）。

### D2 串流用 `fetch`＋`ReadableStream`，解析寫成純函式

新增 `sse-parser.js`（UMD 純函式）：`createParser()` 回傳 `feed(text)`，以 `TextDecoder({stream: true})` 解碼後的字串餵入，依空行（`\n\n`，容許 `\r\n\r\n`）切出事件，取 `event:` 與 `data:`（單行 JSON；多行 `data:` 以換行串接以符合 SSE 標準），以 `:` 開頭的行為註解，忽略；JSON 解析失敗或名稱未知的事件丟掉但不中斷。不完整的尾端留在緩衝區。解碼器放在 `turn-stream.js` 而不是解析器，讓解析器只吃字串，測試時可以直接餵「切在任意位置」的字串。

`turn-stream.js`（UMD，依賴全部注入：`fetch`、`setTimeout`／`clearTimeout`、`AbortController`、`TextDecoder`）提供 `run({url, body, csrf, idleMs, totalMs, signal, onEvent})`，**永不 reject**，回傳 `{ outcome, done, failure }`，`outcome` 為 `done`（收到 `done`）、`error_event`、`interrupted`（連線在 `done` 前結束，含讀取例外）、`timeout_idle`、`timeout_total`、`aborted`（外部取消）、`rejected`（開始前的 JSON 錯誤，附 `failure`）。串流判定完整的唯一依據是 `done`（AGENTS.md 第 5 節），不是 HTTP 狀態、不是 `reader.read()` 的 `done` 旗標。

- **計時器**：每收到任何位元組就重設閒置計時器（含只有 `ping` 的資料塊）；總逾時從送出起算不重設。兩者與外部 `signal` 共用一個內部 `AbortController`；結束原因由「誰先觸發」決定，`finally` 一定清除計時器。
- **CSRF**：`fetch` 不像 axios 自動帶標頭，所以由呼叫端傳入 `Auth.csrfTokenFrom(document)` 的值，標頭 `X-CSRF-Token`；`credentials: 'same-origin'`。
- **開始前的 JSON 錯誤**：`!response.ok` 時讀 JSON，整理成與 `Auth.describeError` 同形的 `{kind, status, code, message, fields}`（純函式 `describeHttpFailure(status, body)`，在 `game-state.js`，測試涵蓋 400／401／403／404／409／429／503）。
- 備選：`EventSource`。拒絕：只能 GET、不能帶本文與 CSRF 標頭、重連行為會重送（會重複預扣），AGENTS.md 也明定用 `fetch`＋`ReadableStream`。

### D3 回合狀態機：串流的畫面只是暫時的，結束後以後端為準

時間軸的資料模型（`game-state.js`，純函式）：每項為 `{key, kind, …}`，`kind` 為 `opening`、`turn`（`client_msg_id` 為 key；欄位 `action`、`story`、`status`、`seq`、`streaming`）、`transition`（一行轉場）、`note`（中斷、通知）。串流中新增一個 `streaming: true` 的 `turn` 項，片段交給**每個氣泡一個** `TypewriterBuffer`，`onUpdate` 寫入該項的 `story`。事件處理：

| 事件 | 處理 |
|---|---|
| `meta` | 記下 `turn_id`（即 `seq`）；`replayed` 為真則標記，之後的單一 `token` 視為整段 |
| `token` | `typewriter.push(text)`，並累積原文（供中斷時判斷是否有內容） |
| `ping` | 無畫面效果（位元組已重設計時器） |
| `transition` | 丟棄目前氣泡（停掉並換新的打字機）、插入一行轉場；`will_retry` 為真則開一個新的串流氣泡，為假則不再開 |
| `state` | 白名單取 `world`（更新面板）與 `notices`（年齡更正，以時間軸 `note` 呈現，含更正前的值）；其餘忽略 |
| `done` | `typewriter.finish()`，`coins` 更新餘額，`status` 為 `ended` 標示完結、`blocked` 解鎖並提示換個行動，然後做**一次 `since_seq` 補齊**以後端內容取代串流暫存的氣泡（以 `client_msg_id` 對應，不閃爍） |
| `error` | 顯示 `message`；氣泡丟棄；仍等 `done` 或連線結束收尾 |

連線在 `done` 前結束（`interrupted`、逾時、取消）：丟棄尚未完成的氣泡、插入一行中斷說明，並提示「這一輪可能已在伺服器完成，已為你重新同步」，再做一次補齊——前端中止不等於後端中止（AGENTS.md 第 5 節）。**被擋與中斷的回合不保存任何部分文字**（對應 `turn_view` 的行為）。

- 備選：串流結束後直接信任串流累積的文字、不再補齊。拒絕：少一次請求，但「畫面上的字」與「資料庫裡的字」可能分歧（例如被取代的回合），而且補齊本來就是離線、換裝置的必經路徑，多走一次反而讓同一條路徑被每輪驗證。成本是每輪多一個小的 GET（只帶 `since_seq`）。

### D4 載入順序與第 0 幕

掛載後依序：①`GET /api/game/sessions/<id>`（快照；同時存入快取）→ ②第 0 幕：`ready`／`fallback` 直接顯示；`pending`／`generating` 呼叫 `opening/stream`（已有同一個 `AbortController` 與計時器邏輯，重用 `turn-stream.run`），409 `opening_in_progress` 時顯示等待說明並每 5 秒重讀快照（最多 24 次，約 2 分鐘，之後顯示重試按鈕）→ ③`since_seq` 補齊歷史 → ④`GET /api/wallet`。③與④不依賴②，與②並行；輸入框在第 0 幕完成前禁用（後端也要求第一個玩家回合須等第 0 幕完成）。

離線（`navigator.onLine` 為假或快照請求網路失敗）：以快取畫出歷史與世界狀態，顯示「離線，僅供閱讀」，送出禁用；恢復連線（`online` 事件）後重新走載入順序。快照 404：清除該存檔的快取並顯示找不到存檔。

### D5 IndexedDB：離線閱讀快取＋單筆在途回合（使用者已確認）

新增 `game-store.js`（UMD，與框架無關，模仿 `cs-store.js` 的結構但**不修改它**：客服已上線、有自己的測試，泛化會讓兩邊互相牽動）。資料庫名稱 `game-cache-<user.public_id>`；集合：`sessions`（key＝存檔識別碼：快照的第 0 幕、世界狀態、標題、完結旗標）、`turns`（key＝`<存檔>|<補零序號>`：`seq`、`client_msg_id`、`turn_number`、`status`、`action`、`story`；被擋與中斷只存狀態）、`inflight`（key＝存檔識別碼：`client_msg_id`、`action`、`created_at`，**每個存檔最多一筆**）。**不存**記事簿全文、email、生日與年齡區間。開啟失敗、被禁止或配額用盡時退回記憶體並照常運作；所有讀寫包 try/catch。

`game-sync.js`（UMD，`api` 與 `store` 注入，模仿 `cs-sync.js`）：`catchUp(sessionId)` 讀本機最大 `seq`，以 `since_seq` 迴圈拉到 `has_more` 為假（單頁 200），後端的回合覆寫本機同序號；回傳是否有變動；網路失敗不拋出（離線）。`resolveInflight(sessionId)`：補齊後若本機有該 `client_msg_id` 的 `complete` 回合就移除在途紀錄，否則回傳在途紀錄供畫面顯示「重送／放棄」。**重送 = 以原 `client_msg_id` 再呼叫 `turns/stream`**（後端冪等：已完成就重播且不碰錢包；未完成才是新的預扣），所以沒有重複扣幣的風險；前端**不**自動重送（每輪 30 幣）。

- 備選 A：只做閱讀快取，不存在途回合。拒絕（使用者選擇）：重新整理會丟掉玩家剛打的長行動。備選 B：照搬客服的待送佇列。拒絕：離線排隊多則行動會各扣 30 幣且後一則依賴前一則的結果。
- 已知限制（記入 README）：IndexedDB 在裝置上，帳號刪除與登出無法由伺服器清除；與客服的快取同一個限制。快取只含劇情與行動，不含記事簿。

### D6 前端模組劃分（全域變數、無打包）

`app/static/js/game/` 新增（載入順序即相依順序，樣板守門測試固定）：`sse-parser.js` → `turn-stream.js` → `game-state.js`（時間軸資料模型、事件到狀態的轉換、錯誤對照、記事簿與簽到的純函式）→ `game-store.js` → `game-sync.js` → `game-wallet.js`、`game-notebook.js`、`game-session.js`（三個 `createXxx(deps)`，回傳 `refs＋methods`，用 Vue 全域的 `ref`／`computed`，依賴由 `game.js` 注入）→ `game.js`（掛載與接線，唯一直接碰 `document`／`window` 的地方）。拆成三個 `create` 是為了讓每個都能在 `vm` 沙盒用替身測試（與 `entry.js`／`wizard.js` 的做法一致），也讓批次的檔案邊界清楚。`game-api.js` 擴充 `snapshot`、`turns`、`wallet`、`checkin`、`notebook`、`saveNotebook`（axios，`timeout` 與 `signal` 由呼叫端傳入）；`game-labels.js` 補串流與記事簿用的對照。樣式在 `game.css` 以 `gm-` 前綴擴充（既有 `ge-`、`gw-`）。

### D7 版面：時間軸＋可開關的側欄，沿用客服的 992px 斷點

側欄（分頁「世界」「記事簿」，寬 320px）**在桌面與手機都預設收合**（使用者決定），頂列有開關按鈕（`aria-expanded`、`aria-label`）。桌面（≥992px）開啟時推擠主區域成兩欄；手機與平板（<992px）以覆蓋方式開啟並附遮罩，Esc 與遮罩可關閉。開關由 Vue 響應式變數接管，不用 `data-bs-*`，**不寫入 localStorage**（客服的 Sidebar 有記，但這裡側欄是資訊面板而非導覽，每次回到預設收合即可；少一個儲存點就少一個個資面）。預設收合讓時間軸在所有寬度都有最大的閱讀空間；代價是世界狀態不會一眼可見，所以頂列在收合時以一行摘要顯示目前地點。時間軸只渲染最近 60 個項目，上方有「顯示更早的內容」；新內容到達時，只有玩家在底部附近（≤80px）才自動捲動，否則顯示「回到最新」。輸入區用 `<textarea>`，Enter 換行、`Ctrl＋Enter` 或按鈕送出；字數上限取自初始狀態的 `limits.action_max`（以 NFC 正規化後的 Unicode 字元計，與後端 `ACTION_MAX` 同一個算法）。

### D8 遊戲幣與簽到

`game-wallet.js`：載入時 `GET /api/wallet`；`balance` 來自 `total`，每輪 `done.coins` 直接覆寫；`turnCost` 取 `turn_cost.standard`（對照表只有這一個鍵，缺值退回不顯示幣價）。餘額不足有兩條路：①送出前已知 `balance < turnCost`：輸入區顯示一行說明與「每日簽到」，**仍允許送出**（伺服器為準，避免快取的餘額過期造成誤擋）；②伺服器 409 `insufficient_coins`：開 Modal。簽到：`POST /api/wallet/checkin`；409 `checkin_overflow` 回預覽（`would_gain`／`would_lose`）→ 兩個按鈕「取消」（不呼叫）「領取」（`confirm_overflow: true`）；409 `already_checked_in` 顯示 `checkin.next_reset_at`（台灣時間）。成功後重讀錢包。**不自動重送**被擋的行動。

### D9 記事簿面板

`game-notebook.js`：開啟面板才 `GET …/notebook`（全文不進快取，也不進初始狀態）；顯示 `text`、`version`、`length`、`capacity`、`reserve`、`full`（即 `notebook_policy.view_of` 的欄位，快照裡的摘要 `summary_of` 沒有全文）。編輯模式持有草稿與開啟時的 `version`；字數以 NFC 正規化後的 Unicode 字元計（與後端同一個算法，純函式放 `game-state.js`，測試與後端 `notebook_policy.length_of` 的取樣案例逐項比對），超過容量禁用儲存。`PUT` 帶 `expected_version`；`notebook_conflict` 回後端最新內容：更新顯示、保留玩家草稿供複製、說明衝突，**不自動合併**。已完結則不顯示編輯。一律純文字（`white-space: pre-wrap`）。

6.1 驗收後補（使用者回報「另一分頁儲存後，舊分頁無法按下儲存、也沒有文字提示」）：①說明（儲存結果、超過容量、尚未修改）與衝突的對照區放在編輯框與按鈕的**下方**，因為對照區是整本記事簿，放在上方會把編輯框與儲存鈕推出視線，並另用 Toast 提醒；②儲存鈕因為尚未修改而灰色時由 `notebookSaveHint` 說明；③編輯中的分頁回到前景（`visibilitychange` 不是隱藏時、視窗 `focus`）時 `checkFresh()` 先 `GET` 一次最新版本（只在編輯中、同時一個請求、失敗安靜、不儲存），沒改字就換成最新內容、改過就保留草稿並對照；④讀取失敗用讀取專用的文字，不沿用儲存失敗那句。這些不改任何後端端點，也不違反「全文只在玩家開啟面板時才讀」（檢查只發生在面板已開且正在編輯時）。

### D10 `/game` 預設分頁

`entry.js` 掛載時並行載入「選劇本」第一頁與「我的存檔」第一頁（後者本來就是切到該分頁才載入，這裡提前一次）；存檔載入完成且非空、玩家**尚未手動切換**（記錄一個 `userPickedTab` 旗標）時切到「我的存檔」。存檔請求失敗則維持「選劇本」，「我的存檔」分頁沿用既有的錯誤與重試。「選劇本」的內容模式提示仍來自目錄回應，兩個請求互不阻擋。這是 `entry.js` 的小改動，放在最後一批。

### D11 隱私與日誌

不新增後端日誌事件；前端不 `console.*` 任何玩家文字（沙盒測試以攔截 `console` 斷言整個載入＋一輪＋記事簿流程）。網址不含玩家文字。IndexedDB 內容不含記事簿全文。錯誤訊息只來自固定文字與列舉對照；伺服器回的 `message` 僅在其為既有的繁體中文固定訊息時顯示（沿用 `Auth.describeError` 的信任範圍），審查類別只顯示列舉的中文名稱。

## Risks / Trade-offs

- [長篇歷史讓時間軸變慢] → 只渲染最近 60 項、其餘「顯示更早的內容」；`since_seq` 單頁 200；暫不做虛擬捲動。
- [串流途中代理緩衝或斷線] → 後端已送 `X-Accel-Buffering: no` 與 15 秒 `ping`；前端 45 秒閒置逾時；補齊機制讓「其實已完成」的回合在下次載入出現。
- [免費模型首字很慢（5 至 35 秒）] → 閒置逾時 45 秒大於實測；`ping` 在狀態呼叫前也會送；真實模型延遲留待正式模型選定後重量。
- [打字機落後於串流，玩家看到字還在跑時 `done` 已到] → `finish()` 在 3 秒內補完；輸入在 `done` 時就解鎖（AGENTS.md：以 `done` 為準），不等打字機。
- [兩個分頁或裝置同時玩同一存檔] → 後端 `turn_in_progress` 409 與版本檢查；前端說明並補齊，不自動重送。
- [快取與後端分歧] → 一律以後端覆寫；補齊後才畫面定稿；404 清除。
- [瀏覽器端無法被伺服器清除] → 與客服相同的已知限制，README 記載。
- [本批只靠 `vm` 沙盒與 Flask 測試用戶端，沒有真實瀏覽器] → 瀏覽器實測（驗收環境＋假 RPG 模型，走第 0 幕、一輪、被擋一次、被擋兩次、中斷、慢速、結局、記事簿超量、簽到）由使用者親自操作，結果如實記入 README，做不到的標「未驗證」。

## Migration Plan

沒有遷移、沒有新設定、沒有部署順序限制。回滾＝還原程式（`/game/<存檔>` 回到空殼）；使用者端的 IndexedDB 資料庫留在裝置上但無人讀取，無害。

## Open Questions

- 後端的行動上限（1000 字）原本是「與客服一致的假設」，RPG 沒有明文規範；本 change 不改它，只讓前端引用。若日後要調整，屬後端 change。
- 「我的存檔」是否顯示最後一幕摘要或封面（`docs/RPG.md` 7.17.6 的 RC-3b 項目 ④）：不影響契約，列為不做，留給之後的畫面調整。
