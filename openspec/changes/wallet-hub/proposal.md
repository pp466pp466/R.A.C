# Proposal

## Why

遊戲幣是 RPG 每一輪都要用到的資源，但玩家現在只有在「遊戲進行頁」的頂列看得到餘額；入口頁 `/game`、創角頁、客服、劇本創作與帳號頁都看不到。更大的缺口是**每日簽到**：簽到按鈕只出現在進行頁「餘額低」的提示與 409 `insufficient_coins` 的 Modal 裡，餘額夠的玩家在任何頁面都找不到簽到，每天的免費幣因此領不到。帳本的記錄 API（`GET /api/wallet/entries`）也早已存在，卻沒有任何畫面。

另外，本平台日後要有付費幣（資料庫與服務已有 `paid_balance`，扣款順序先免費後付費已定），但儲值的金流、商品、價格與退款規範都還沒有（AGENTS.md 第 7 節：「儲值與訂閱的規範待金流探索後另訂」）。展示時需要向對方說明「變現的可能性」，所以需要一個看得出儲值會長什麼樣、又不會讓人誤以為已經可以購買的示意畫面。

這個 change 做三件事：讓每一頁頂列都看得到餘額、提供一個錢包頁（`/wallet`）作為簽到、記錄與儲值方向的入口、把簽到入口補到每一頁都找得到。

## What Changes

- **共用的餘額標籤**：新增全站共用的錢包狀態庫（`ui/wallet-store.js`，風格同 `toast-store.js`，Node 可測）與元件 `<ui-wallet-balance>`，由 `UI.install(app)` 註冊。標籤顯示合計餘額，**可點擊，連到 `/wallet`**；載入中與讀取失敗顯示「—」，不阻擋頁面；管理員（`coin_exempt` 為真）顯示真實餘額加小標籤「不消耗」。同一頁只呼叫一次 `GET /api/wallet`（標籤與頁面自己的邏輯共用同一份狀態）。
- **標籤放進六個頁面的頂列**：`/game`、`/game/start/<版本>`、`/game/<存檔>`（取代現有的 `gm-balance`）、`/cs`、`/scripts`、`/account`（沒有頂列，放進 `account-nav` 那一列的右側）。不放在登入前與限制狀態的頁面（`/login`、`/age`、`/appeal`、法律頁、`/admin`）。手機寬度只顯示「幣 N」。
- **新頁面 `/wallet`（錢包頁）**：MVC Page Controller 只渲染外殼（沿用既有的存取規則，受限狀態導向既有的頁面），資料由前端呼叫既有 API。內容：
  - 頂部：合計餘額、「付費 N｜贈送 N」拆分（免費幣在畫面上稱為「贈送」）、右上「紀錄 ›」。
  - **「免費」分頁**：每日簽到卡（可領取時顯示預覽與「領取」，超過免費幣上限時沿用「取消／領取」確認；今日已簽到時顯示距離下次簽到的倒數 `時:分:秒`）、註冊贈幣卡（只說明「完成年齡確認時自動發放一次」，沒有領取按鈕）。
  - **「儲值」分頁（示意）**：幾張幣包卡片，標明「示意，非正式價格」，按鈕一律停用顯示「即將推出」，**不發出任何增加餘額的請求**。示意資料只放在一個前端資料模組，不進資料庫、設定表與 API。
  - **「紀錄」檢視**：由新到舊的帳本清單（接 `GET /api/wallet/entries`，游標分頁，「載入更多」），顯示說明、免費與付費增減量與時間（台灣時間）；尚未結算的預扣顯示「處理中」。
  - 使用說明（幣的用法：先扣贈送再扣付費、不過期、免費幣上限）。
- **簽到邏輯共用化**：把 `game-wallet.js` 內的簽到狀態機（預覽、取消、領取、已簽到、重複點擊）抽成共用模組，進行頁與錢包頁共用；進行頁保留「餘額不足」提示與 409 Modal。`Game.describeCheckinFailure` 保留為薄轉接，既有測試不變。
- **不變的事**：沒有新的 API、資料表、欄位或 migration；`GET /api/wallet`、`POST /api/wallet/checkin`、`GET /api/wallet/entries` 的契約不變；扣款、簽到的伺服器規則不變；玩家仍無法經由 HTTP 增加餘額（簽到除外）。
- **文件**：`docs/前端結構-frontend.md`（共用元件與錢包頁）、`docs/系統架構-architecture.md`（頁面路由、前端模組）、`docs/RPG.md`（遊戲幣一節的畫面說明）、`docs/路線圖-roadmap.md`、`docs/README.md` 的對照表檢查。簡報（頁面路由 19→20、前端架構與網站入口章節）依 `deck-conflict-watch` 只列出、由使用者決定，不在本 change 自行改。
- **BREAKING**：無。

## Capabilities

### New Capabilities

- `shared-wallet-balance`：全站共用的餘額標籤與錢包狀態庫：顯示規則（合計、「—」、管理員的「不消耗」、手機寬度）、點擊前往錢包頁、同一頁只請求一次、純文字渲染，以及放置的六個頁面。
- `wallet-hub-page`：`/wallet` 錢包頁：路由與存取規則、頂部餘額與拆分、免費分頁（簽到與倒數、註冊贈幣說明）、儲值分頁（示意）的界線、紀錄檢視、使用說明，以及整頁的前端規範（純文字、自製 Modal 與 Toast、不使用 Bootstrap JavaScript）。

### Modified Capabilities

- `rpg-wallet-ui`：遊戲頁頂列的餘額改由共用標籤顯示（行為不變：載入顯示、每輪 `done.coins` 更新、失敗顯示「—」、管理員不顯示餘額不足提示）；簽到入口的需求拿掉「不提供記錄頁」的限制（記錄頁改由錢包頁提供，儲值仍不提供真實購買）。

## Impact

- **程式（後端）**：新增 `app/controllers/wallet_page_controller.py`（Blueprint，`GET /wallet`，`login_required_page`）並在 `app/__init__.py` 註冊；新增 `app/templates/wallet/hub.html`；各頁頂列樣板加一個標籤。**沒有 Service、Repository、API 異動。**
- **程式（前端）**：新增 `app/static/js/ui/wallet-store.js`、`ui/wallet-checkin.js`（簽到狀態機）、元件註冊於 `ui/ui-components.js`；新增 `app/static/js/wallet/` 下的錢包頁模組與示意資料模組；新增 `app/static/css/wallet.css`；調整 `game/game-wallet.js`（改寫入共用狀態庫並委派簽到）、`game/game-state.js`（`describeCheckinFailure` 轉接）、`base.html` 載入順序、`cs/chat.html`、`scripts/workshop.html`、`game/index.html`、`game/start.html`、`game/session.html`、`auth/account.html`。
- **API**：無異動。
- **資料庫**：無異動，不需要 migration，不需要更新 `docs/project資料結構文件.md`。
- **文件**：見上。
- **測試**：Node（錢包狀態庫、簽到狀態機、倒數與紀錄格式化、錢包頁狀態、示意資料的守門）、pytest（`/wallet` 路由與受限狀態導向、六個頁面都有標籤、錢包頁沒有寫入類的請求、`tests/architecture` 的前端規則）。
- **風險**：①示意價格若被誤認為真實價格（以標示、停用按鈕與守門測試降低；上線給真實玩家前須換掉或拿掉）；②每頁多一次 `GET /api/wallet`（它不是純讀取，會建錢包、補贈幣、退逾時預扣，但皆冪等）；③簽到狀態機抽出時不得改變既有行為（以既有的 Node 測試把關）。
