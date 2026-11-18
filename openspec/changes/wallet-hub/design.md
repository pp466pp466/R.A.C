# Design

## Context

動機與範圍見 `proposal.md`；行為契約見 `specs/`。這個 change 沒有後端 Service、Repository、API、資料表與 migration 的異動，重點在前端的共用層與一個新頁面。

現況（已讀過程式）：

- **餘額只在進行頁**：`game/session.html` 的 `gm-balance` 顯示 `balanceText`；`game/index.html`、`game/start.html`（`ge-topbar`）、`cs/chat.html`（`cs-topbar`）、`scripts/workshop.html`（`sw-topbar`）沒有；`auth/account.html` 沒有頂列，只有頁內 `account-nav`（目前一個連結），且有「等待刪除」的簡化分支。
- **簽到與餘額邏輯全在 `game/game-wallet.js`**（`Game.createWallet(deps)`，Node 可測）：持有 `balance`、`exempt`、`turnCost`、`nextResetAt`，以及 Modal 狀態與整個簽到狀態機（預覽、取消、領取、已簽到、重複點擊）。失敗訊息來自 `Game.describeCheckinFailure`（`game-state.js`），它又依賴 `game-labels.js` 與 `game-state.js` 內部的失敗分類。
- **API 都在**：`GET /api/wallet`（`total`、`free_balance`、`paid_balance`、`free_cap`、`turn_cost`、`coin_exempt`、`checkin`）、`POST /api/wallet/checkin`、`GET /api/wallet/entries`（`entries`、`next_before`；每筆含繁體中文 `label`、`pending`、`free_delta`、`paid_delta`、`created_at`、`seq`）。`GET /api/wallet` 不是純讀取（建錢包、補贈幣、退逾時預扣），皆冪等。
- **共用元件**：`base.html` 載入 `ui-components.js`，裡面建立全站的 toast 單例（`UI.toasts`），並由 `UI.install(app)` 註冊 `<ui-modal>`、`<ui-toast-region>`、`<ui-restriction-notice>`；六個目標頁面都已呼叫 `UI.install(app)`。
- **頁面都是整頁換頁**（不是單頁應用），每頁一個獨立的 Vue app。HTML 頁面一律 `Cache-Control: no-store`（`cache_headers.py`）。
- **既有守門**：`game-api.js` 的寫入呼叫恰好四個（`suggest`、`start`、`checkin`、`saveNotebook`，`tests/test_game_pages.py` 以原始碼固定）；`tests/architecture/test_frontend_rules.py` 掃所有樣板與 JS（離線、無 `v-html`、無 `data-bs-*`、無 CDN）。

## Goals / Non-Goals

**Goals:**

- 六個頁面的頂列有同一個餘額標籤，來源單一、同一頁只請求一次、行為一致。
- 每日簽到在任何頁面都找得到入口（標籤 → 錢包頁 → 免費分頁）。
- 錢包頁把「免費」「儲值（示意）」「紀錄」放在同一處，且沒有任何路徑能增加餘額（簽到除外）。
- 不改變進行頁既有的餘額不足提示、409 Modal 與簽到行為；既有 Node 測試不需修改語意。

**Non-Goals:**

- 不做真實的儲值、訂閱、金流、價格、退款（AGENTS.md 第 7 節待金流探索後另訂）；示意資料不是規範。
- 不新增 API、資料表、設定鍵或 migration；不顯示註冊贈幣的數量（API 沒有這個欄位，不為此改 API）。
- 不做跨頁或跨分頁同步（餘額在換頁時重讀；另一個瀏覽器分頁的變動不即時反映）。
- 不把餘額快取到瀏覽器儲存。
- 不重構 `game-state.js` 的失敗分類（見 D4 的取捨）。

## Decisions

### D1：共用狀態庫放在 `ui/`，風格同 `toast-store.js`

新增 `app/static/js/ui/wallet-store.js`：工廠 `createWalletStore({ reactive, fetcher })`（與 `createToastStore` 同樣，瀏覽器注入 `Vue.reactive`，Node 測試用一般物件與假的 fetcher），`ui-components.js` 建立全站單例 `UI.wallet`。狀態：`status`（`idle`／`loading`／`ready`／`failed`）、`total`、`free`、`paid`、`exempt`、`freeCap`、`turnCost`、`checkin`（`claimable`、`grant`、`wouldGain`、`wouldLose`、`nextResetAt`）。方法：`load()`（並行合併、永不拋出）、`apply(wallet)`（驗證型別後寫入；簽到成功與 `GET` 共用）、`setTotal(n)`（`done.coins`）。

- **為什麼放 `ui/`**：標籤要在六個不同的 Vue app 裡用，而 `ui-components.js` 已經是所有頁面共同載入的位置，`UI.install(app)` 註冊元件的模式也已建立；`Game.*` 只在遊戲頁載入。
- **備選：每頁各自請求並各自存放**。→ 六份重複邏輯，進行頁還會重複請求。否決。
- **備選：以 `sessionStorage` 快取讓換頁沒有「—」閃爍**。→ 顯示過期數字比顯示「—」更糟，而且要處理登出後殘留；規格也明定不保存。否決，接受換頁時短暫的「—」。
- 預設 `fetcher` 是 `ui/wallet-api.js` 的 `wallet()`（axios，離線引入），錯誤只用於把狀態設為 `failed`，不向外輸出內容。

### D2：標籤元件自己觸發載入，頁面邏輯共用同一份

`<ui-wallet-balance>`（註冊於 `UI.install`）在 `onMounted` 呼叫 `UI.wallet.load()`；`load()` 在 `loading` 或 `ready` 時直接回傳現有的承諾，所以標籤與頁面自己的邏輯（`game-wallet.js`）同時要求時只有一個請求。標籤是 `<a href="/wallet">`，內含純文字數字、手機寬度會隱藏的「遊戲幣」字樣，以及管理員的「不消耗」小標示。

- 頁面只需在頂列放一個標籤，不必各自接資料。
- `ready` 之後的重新讀取只由明確的動作觸發（倒數歸零、重試），不自動輪詢。

### D3：`game-wallet.js` 改為讀寫共用狀態庫，對外介面不變

`Game.createWallet(deps)` 新增選用的 `deps.store`（沒給就用 `UI.createWalletStore` 建一份本地的，Node 測試不必改）。`balance`、`exempt`、`turnCost`、`nextResetAt` 改為從 store 衍生（`computed`），`load()` 轉呼叫 `store.load()`，`done.coins` 與簽到結果經 `store.setTotal`／`store.apply` 寫入。回傳的物件欄位與方法維持不變，所以 `game.js`、樣板與既有的 `game-wallet.test.js` 只需要小幅調整。`gm-balance` 以標籤取代；樣板的輸入區提示用的是 `lowBalanceText`（已含餘額數字），`balanceText` 不再被樣板使用，從樣板約定與 `game.js` 的回傳移除（模組本身仍提供）。

- 這讓頂列標籤與輸入區的「餘額不足」提示永遠依同一份數字，不會分歧（對應 `rpg-wallet-ui` 的新場景）。

### D4：簽到狀態機抽成共用模組，失敗訊息沿用 `Game.describeCheckinFailure`

新增 `ui/wallet-checkin.js`：`createCheckin({ Vue, api, store, describeFailure, hooks })`，持有 `busy`、`preview`、`message`、`finished`，提供 `claim()`、`confirmOverflow()`、`cancelOverflow()`，行為與 `game-wallet.js` 現行的一致（重複點擊只一個請求、`finally` 恢復、取消不送請求、領取才帶 `confirm_overflow`）。與介面相關的部分用 `hooks`（`onOverflow`、`onAlready`、`onError`、`onSuccess`、`onCanceled`，另有 `onFailure`、`onLogin`）交給呼叫端：進行頁把它們接到既有的簽到 Modal，錢包頁接到頁內的簽到卡。實作（批 ②）時的細節：`onOverflow`／`onAlready`／`onError` 在預覽與說明寫入**之前**呼叫（進行頁在這裡打開 Modal，且 Modal 已開著時沿用）；`onSuccess(current)` 與 `onCanceled(text)` 的回傳值讓頁面取代預設說明（字串）或表示已自行呈現（`true`）；`onFailure(info)` 收分類前的原始失敗（遊戲頁用來辨識「被鎖定」）；模組另提供 `reset(options)`（重新打開簽到畫面時依錢包重判 `finished`；批 ③ 加了選用的 `{ authoritative: true }`：呼叫端剛從伺服器重讀了錢包（錢包頁倒數歸零、重試），那份快照比「這個頁面看過今天簽過了」的記憶新，所以先忘掉記憶，新的一天才能再簽到；不帶參數時行為不變）與 `dropPreview()`（呼叫端的視窗被 Esc 或遮罩關掉時清預覽）。模組只由需要簽到的頁面載入（進行頁、錢包頁），不放進 `base.html`。

- **為什麼不把 `describeCheckinFailure` 一起搬走**：它依賴 `game-state.js` 內部的 `fromInfo`、`transportFailure`、`gateFailure` 與 `game-labels.js` 的文字；搬動會牽動整個失敗分類，風險與範圍遠大於這個 change。改為由呼叫端注入 `describeFailure`，錢包頁載入 `game-labels.js` 與 `game-state.js`（兩者都是不碰 DOM 的純模組）只為了取得 `Game.describeCheckinFailure` 與台灣時間的格式化函式。這個耦合寫進程式註解與測試，日後若要拆除，是另一個重構 change。
- **備選：錢包頁直接用 `Game.createWallet`**。→ 要連 `game-api.js`（整套遊戲 API）一起載入，也把 Modal 狀態拖進一個沒有 Modal 的頁面，並且會動到「寫入呼叫恰好四個」的守門。否決。
- **備選：兩邊各自維護一份簽到流程**。→ 規格要求兩處行為一致，重複實作必然分歧。否決。

### D5：錢包頁的 API 呼叫放新的 `ui/wallet-api.js`，不動 `game-api.js`

新增 `UI.walletApi`：`wallet()`、`checkin(confirmOverflow)`、`entries(before, limit)`，用離線的 axios；失敗以 `Auth.describeError` 整理後補上簽到預覽（`checkin.would_gain`／`would_lose`）、`balance`、`needed` 這幾個固定形狀的整數欄位，與 `game-api.js` 的 `reject` 同樣的形狀，所以 `Game.describeCheckinFailure` 可直接使用。**`game-api.js` 一個字不改**，它的 `wallet`／`checkin` 繼續存在（進行頁的簽到仍走它，守門測試的「恰好四個寫入」原封不動）。

- 代價是 `reject` 的小段整理邏輯有兩份（約十行）。接受，換取不動既有的守門與進行頁行為；`tests/` 加一個測試對照兩份整理邏輯對同樣輸入產生相同的形狀。
- 錢包頁只依賴 `auth-common.js`（`Auth.describeError`），與其他頁面一致。

### D6：新頁面 `/wallet`——薄的 Controller、一個樣板、獨立的 JS 與 CSS

新增 `app/controllers/wallet_page_controller.py`（Blueprint `wallet_pages_bp`，`GET /wallet`，`@login_required_page`，回傳 `wallet/hub.html`，初始狀態只有 `member_tier` 之類不敏感的值，**不放餘額**），在 `app/__init__.py` 註冊。受限狀態的導向由 `login_required_page` 既有的預設處理（鎖定 `/appeal`、待確認年齡 `/age`、等待刪除 `/account`），Controller 不寫權限判斷。樣板 `wallet/hub.html` 延伸 `base.html`，載入 `css/wallet.css` 與 `js/wallet/` 下的模組。

- **為什麼 Controller 不順便讀餘額放進初始狀態**：`GET /api/wallet` 有寫入的副作用（補贈幣、退預扣），頁面渲染不該做寫入；API 路徑也已有 `require_login`、CSRF 與錯誤處理。
- 分頁與紀錄檢視是頁內的 Vue 狀態，**不新增路由**（`/wallet` 只有一個）。

### D7：錢包頁的前端模組切分（Node 可測的純函式與 DOM 分離）

- `wallet/wallet-hub-state.js`（純函式）：分頁與檢視狀態、倒數的計算與格式化（`時:分:秒`）、紀錄項目的整理（增減量帶正負號、時間格式化）、`載入更多` 的游標管理、重讀的節流判斷。
- `wallet/wallet-demo-packages.js`：**示意幣包資料，唯一的位置**，檔頭註明「示意，非正式價格；上線給真實玩家前須換掉或拿掉」，並匯出一個常數旗標 `IS_DEMO = true`。
- `wallet/wallet-hub.js`：Vue app 組裝，只接線不放規則。

### D8：倒數以時間相減計算，歸零只重讀一次並節流

剩餘時間 = `next_reset_at` − 目前時間（每秒由計時器觸發重新計算，而不是每秒減一，所以背景分頁被節流後恢復仍正確）。歸零時呼叫 `store.load()` 一次；若重讀後 `claimable` 仍為 `false` 且 `next_reset_at` 仍在過去（本機時鐘比伺服器快），60 秒內不再重讀。計時器在離開簽到卡的檢視、頁面卸載時清除。實作（批 ③）：計時器只在「主檢視、免費分頁、今天已簽到、下次時間可用」時啟動，由預設時機（非同步）的 `watch` 啟停——不用同步時機，因為狀態庫的 `apply` 是一欄一欄寫入的，同步觸發會在寫到一半（已簽到但下次時間還是舊的）誤判歸零；重讀後以 `reset({ authoritative: true })` 讓簽到狀態機採用新快照（見 D4）。

- **為什麼不信任本機時鐘就多問一次伺服器**：簽到的「今天」由伺服器的台北時間日界決定，本機時鐘只能用來顯示。節流避免時鐘錯誤時無限重讀。

### D9：「儲值」分頁的示意界線（A2）

分頁內容完全由 `wallet-demo-packages.js` 的資料渲染成卡片；每張卡與分頁頂端都標「示意，非正式價格」；按鈕 `disabled` 並顯示「即將推出」，**沒有 `@click` 綁定任何請求**。守門測試：①錢包頁所有模組中非 GET 的請求恰好一個（簽到）；②示意資料模組以外的樣板、後端與設定中沒有幣包數字；③`IS_DEMO` 為真時畫面一定出現示意標示。

- **備選：說明型（只有文字，沒有卡片）**。→ 使用者在探索時選擇示意卡片，因為展示需要讓對方看出儲值的樣貌。保留為決定。
- **風險的緩解**：見下。上線前的處置寫進風險段與路線圖。

### D10：預設分頁為「免費」，紀錄是頁內檢視

預設顯示「免費」，因為那是玩家唯一能「做事」的分頁（簽到）。「紀錄」從頂部入口進入，隱藏分頁列並顯示「返回」；返回後回到先前的分頁。紀錄清單的狀態保留在頁內記憶體（返回再進入時不重新請求，除非玩家按重試），離開頁面即丟棄。

### D11：`/account` 的放置與受限分支

`account.html` 的 `account-nav` 目前在 `v-else`（正常）分支；標籤放在該導覽列右側，「等待刪除」等簡化分支不放（避免對一個即將刪除的帳號顯示餘額並發出請求）。管理員在未完成年齡確認時仍可進入 `/account`；`GET /api/wallet` 對管理員不擋，標籤照常顯示。

### D12：樣式與手機寬度

新增 `css/wallet.css`（錢包頁）；標籤的樣式放進共用的 `components.css`（`ui-wallet-balance`），只用 `theme.css` 的變數（背景 `#0F1012`、文字 `#E2E4E9` 與 `#8A8F99`、強調 `#FF6B35`、分隔線 `#2A2D34`）。未滿 576px 時隱藏「遊戲幣」字樣，只留「幣 N」。示意卡片的外觀參考分類（幣數、贈送、價格、按鈕），但用本專案的色彩規範與繁體中文用語（遊戲幣，不是任何其他產品的名稱與品牌色）。

## Risks / Trade-offs

- [示意價格被誤認為真實價格，或日後忘了拿掉] → 分頁與卡片雙重標示、按鈕停用、示意資料集中在單一模組並有 `IS_DEMO` 旗標與守門測試；風險寫進路線圖，**上線給真實玩家前必須換掉或拿掉**（屬 `wallet-topup` 的前置）；內容規範與標示方式列入法務覆核。
- [每頁多一次 `GET /api/wallet`，且它會寫入（補贈幣、退預扣）] → 皆冪等且有界；進行頁原本就會請求，不增加；其他頁每次載入一次，量級與既有頁面的其他 `GET` 相當。
- [抽出簽到狀態機時改變既有行為] → 以既有的 `game-wallet.test.js`、`game.test.js` 把關，新增「兩處行為一致」的對照測試；先抽出並讓既有測試全過，再做錢包頁。
- [錢包頁載入 `game-state.js` 與 `game-labels.js` 造成耦合與多餘的載入] → 兩者是純模組、體積小且會被瀏覽器快取；耦合記在註解與測試，不在本 change 處理（D4）。
- [`reject` 整理邏輯兩份] → 以對照測試保證形狀一致（D5）。
- [倒數的本機時鐘誤差] → 只用於顯示，歸零重讀並節流（D8）。
- [標籤的「—」在換頁時短暫閃爍] → 接受；不快取（D1）。
- [管理員的零金額預扣讓紀錄清單出現許多 0 的列] → 與一般玩家同量級，紀錄分頁載入；不隱藏，因為那是帳本的事實（admin-unlimited-coins 的設計）。
- [`/wallet` 是新的頁面路由，簡報的頁面數與架構章節會過時] → 依 `deck-conflict-watch` 通知使用者，由使用者決定，不自行改。

## Migration Plan

沒有資料庫、設定鍵與 migration 異動，不需要備份步驟。部署即生效（靜態檔隨映像更新）；回復只需部署上一版映像。實作分五批（使用者已確認）：①共用狀態庫、標籤元件、六個頂列、`game-wallet.js` 的整合（含 360 px 版面的提前量測）；②簽到狀態機抽出（純重構）；③錢包頁骨架與免費分頁；④儲值（示意）與紀錄；⑤驗證、瀏覽器實測、全套測試、文件與簡報通知。

## Open Questions

- 示意幣包的幣數與價格要擬幾張、怎麼擬：實作時由使用者在批 ③ 前確認（不影響規格：規格只規定標示、停用與資料集中）。
- 錢包頁的色彩與卡片細節以瀏覽器實測為準，由使用者驗收時調整。
