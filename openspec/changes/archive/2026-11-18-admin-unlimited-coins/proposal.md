# Proposal

## Why

營運需要用管理員帳號反覆測試 RPG 的模型與劇情品質（回合、重新生成、編輯），但每一輪要扣 30 幣：管理員不會被發放贈幣與簽到（`game-wallet` 規格已寫「管理員不發放」），餘額永遠是 0，目前一輪都玩不了，只能靠「管理員調整餘額」手動補幣，既要重新輸入密碼、又會在帳本留下大量人工調整，也讓真實的幣流分析混入測試。

AGENTS.md 第 7 節「遊戲幣（RPG）」已於 2026-11-18（`9232f18`）修訂：管理員帳號不消耗遊戲幣，預扣仍寫入帳本但金額為 0；客服額度與全站 token 上限不受影響；豁免只依伺服器端角色判斷。規範已定，這個 change 把它實作出來。

## What Changes

- **預扣對管理員記 0 幣**：`WalletService.hold` 對「角色為 admin 且帳號啟用中」的使用者，不檢查餘額、不扣任何餘額，仍照常寫入一筆 `turn_hold` 帳本列（免費與付費增減量皆為 0），冪等鍵、結算、退回與逾時釋放的流程完全不變（結算與退回的金額自然為 0）。回合與修訂（重新生成、編輯）都走同一個 `hold`，所以一個改動涵蓋三條路徑。
- **判斷只在伺服器、只有一個地方**：把「角色為 admin 且帳號啟用中」收斂為 `CurrentUser` 的單一屬性（`is_active_admin`），`AccessPolicy.is_admin` 與錢包服務共用，不新增第二套判斷；不接受任何前端參數、請求欄位或會員等級作為輸入。
- **API 回應讓畫面知道不扣幣**：`GET /api/wallet` 新增布林欄位 `coin_exempt`，管理員的 `turn_cost.standard` 為 0；`last_turn` 的 `regenerate_cost` 與 `edit_cost` 對管理員一律為 0。一般玩家的回應不變（`coin_exempt` 為 `false`）。
- **畫面配合**：管理員不顯示「餘額不足」提示，編輯與重新生成不因餘額停用，用完免費次數後顯示「管理員不消耗遊戲幣」而不是幣價或「會消耗遊戲幣」。餘額本身照常顯示（管理員仍可用「調整餘額」測試扣款路徑，只是不會被回合扣）。
- **不變的事**：免費次數照常計數（成功才計次，管理員的修訂與一般玩家走同一套認領與計次）；客服每日對話輪數、每分鐘訊息數、對話數上限與全站每日 token 上限不受影響；模型呼叫照常寫入 `llm_call_logs`；管理員仍不發放首次註冊贈幣與簽到以外的任何東西；`admin_adjust` 不變。
- **文件**：`docs/RPG.md` 遊戲幣一節與設計文件對照表中受影響者同步；簡報第 109、626、779、781 行與 RPG 回合圖的預扣說明可能需補「管理員不扣幣」，依 `deck-conflict-watch` 只列出、由使用者決定，不在本 change 自行改。
- **BREAKING**：無。沒有資料表、欄位或 migration 異動；既有帳本、錢包與 API 欄位向下相容（只新增一個欄位）。

## Capabilities

### New Capabilities

（無。管理員的豁免是既有錢包行為的例外，併入既有的 capability。）

### Modified Capabilities

- `game-wallet`：預扣對管理員記 0 幣、不檢查餘額、帳本照寫；判斷只依伺服器端角色與帳號狀態。
- `wallet-api`：`GET /api/wallet` 新增 `coin_exempt`，管理員的每輪幣價為 0。
- `rpg-turn-engine`：回合開始預扣的需求補上管理員的例外（不因餘額不足被拒、沒有 `insufficient_coins`）。
- `rpg-turn-revision`：付費的修訂對管理員預扣 0 幣，`last_turn` 的修訂費用為 0。
- `rpg-wallet-ui`：管理員（`coin_exempt`）不顯示送出前的餘額不足提示。
- `rpg-revision-ui`：免費次數用完後，管理員看到「不消耗遊戲幣」而不是幣價，編輯不因餘額停用。

## Impact

- **程式**：`app/services/current_user.py`（單一屬性）、`app/services/access_policy.py`（`is_admin` 改為委派）、`app/services/wallet_service.py`（`hold`、`WalletView`）、`app/services/game_service.py`（`_last_turn_view` 的兩個費用）、`app/static/js/game/game-wallet.js`、`game-revision.js`、`game-labels.js`、`game-state.js`（讀取 `coin_exempt` 與文字）、`app/templates/game/session.html`（若有固定文字）。
- **API**：`GET /api/wallet` 回應新增一個欄位；`last_turn` 的值對管理員為 0。
- **資料庫**：無異動，不需要 migration，不需要更新 `docs/project資料結構文件.md`。
- **文件**：`docs/RPG.md`、`docs/前端結構-frontend.md`（錢包模組）、相關設計文件；簡報只通知。
- **測試**：錢包服務、回合與修訂（管理員與一般玩家對照）、`/api/wallet` 回應、前端純函式。
