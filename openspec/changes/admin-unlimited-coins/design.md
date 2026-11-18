# Design

## Context

動機與範圍見 `proposal.md`；行為契約見 `specs/`。規範來源：AGENTS.md 第 7 節「遊戲幣（RPG）」（2026-11-18，`9232f18` 已寫入「管理員帳號不消耗遊戲幣」，不需再改 AGENTS.md）。

現況（已讀過程式）：

- **所有扣款都經 `WalletService.hold`**：回合（`GameService.begin_turn`，約第 384 行）與付費的修訂（`begin_revision`，約第 493 行）都呼叫它；結算與退回（`settle`／`refund`）只看帳本列的增減量，金額為 0 時自然沒有任何餘額變動。回合引擎與修訂執行器不看幣價，只帶著 `hold_key` 與 `cost` 往下傳。
- **`hold` 的流程**：驗證鍵與存檔識別碼 → 讀幣價 → `_prepare`（建錢包、補贈幣、退逾時預扣；管理員本來就不補贈幣）→ 以鍵查既有預扣 → `_transact` 內的 `plan` 檢查合計餘額、拆分免費與付費、寫 `turn_hold` 帳本列。
- **帳本與錢包沒有「增減量必須非零」的約束**：只有 `balances_non_negative`；`WalletRepository.apply` 沒有對零增減量特別處理。所以 0 幣的預扣列是合法資料，不需要 migration。
- **管理員判斷**：`AccessPolicy.is_admin` 以 `role == admin and status == active` 判斷，`CurrentUser` 帶 `role` 與 `status`；`WalletService` 目前只用 `current.role == ROLE_ADMIN` 擋贈幣。
- **畫面**：`game-wallet.js` 以 `turn_cost.standard > 0` 才設 `turnCost`，`lowBalance` 在 `balance < turnCost` 時為真；`game-revision.js` 的 `priceOf` 在幣價為 0 時回傳 `null`，`editInsufficient` 因此為假，但文字函式在 `cost == null` 時會寫「會消耗遊戲幣」。
- **不需動的**：`_last_turn_view` 的 `free_left` 與免費次數的認領、計次（管理員與一般玩家共用）；`done.coins` 是讀餘額（管理員為實際餘額，通常是 0）。

## Goals / Non-Goals

**Goals:**

- 管理員在 RPG 的回合、重新生成、編輯都不被扣幣、不因餘額不足被拒，且帳本仍有可追溯的紀錄（金額為 0）。
- 豁免的判斷只在伺服器、只有一處，與 `AccessPolicy.is_admin` 同一個定義。
- 畫面不對管理員顯示會消耗幣或餘額不足的文字。

**Non-Goals:**

- 不改客服的任何額度、全站 token 上限與 `llm_call_logs`；不改首次贈幣、簽到與管理員調整。
- 不讓管理員跳過免費次數的計數（修訂的認領、計次與一般玩家共用一套，只是超過免費次數後的預扣為 0）。
- 不新增資料表、欄位、設定鍵或 migration；不新增帳本種類（沿用 `turn_hold`）。
- 不在畫面上隱藏管理員的餘額。

## Decisions

### D1：在 `hold` 內處理，而不是在呼叫端略過 `hold`

`WalletService.hold` 一開始（驗證鍵之後、讀幣價之前或之後皆可）判斷 `current.is_active_admin`：是的話，`plan` 回傳「餘額不變、增減量 0」的 `turn_hold` 草稿，`Hold.cost` 為 0。冪等重送、結算、退回、逾時釋放與記錄頁完全沿用。

- **為什麼不在 `GameService` 略過 `hold`**：規範明寫「預扣仍寫入帳本但金額為 0」；略過會讓管理員的回合沒有預扣列，結算與退回、冪等鍵（重送不重複生成的依據之一）與逾時接手的邏輯都要分叉，三條路徑（回合、重新生成、編輯）要各改一次，容易漏。
- **為什麼不改 `coin_turn_cost` 為 0**：幣價是設定值（對全站），不是依使用者；改它會影響一般玩家。
- 管理員的預扣是否仍要先過 `_prepare`：要。它對管理員只會建立空錢包與退逾時的預扣，成本低，也讓管理員先前（豁免前）遺留的逾時預扣會被退回。

### D2：判斷放在 `CurrentUser.is_active_admin`，`AccessPolicy.is_admin` 委派給它

新增一個屬性：`role == ROLE_ADMIN and status == STATUS_ACTIVE`。`AccessPolicy.is_admin` 改為 `return user.is_active_admin`，`WalletService` 直接用屬性。

- **為什麼不放在 `access_policy.py` 的函式**：`WalletService` 不持有 `AccessPolicy`，為了一個判斷把政策物件接進錢包服務（`registry` 與測試的建構式都要改）不划算；`CurrentUser` 已經帶著 `role` 與 `status`，已有類似的屬性（`restriction`、`may_delete_account`）。
- 輸入只有伺服器端的使用者紀錄（`CurrentUser.from_model`），沒有任何請求欄位能影響；會員等級不參與。
- 測試要鎖住「`AccessPolicy.is_admin` 與 `is_active_admin` 對所有角色、狀態組合結果相同」，避免日後兩邊分歧。

### D3：API 以 `coin_exempt` 告知畫面，幣價對管理員回 0

`WalletView` 新增 `coin_exempt: bool`，`to_dict` 輸出；管理員的 `turn_cost` 各鍵為 0（一般玩家為設定值）。`_last_turn_view` 的 `regenerate_cost` 與 `edit_cost` 對管理員為 0（`GameService` 已有 `current`，用同一個屬性）。

- **為什麼要新欄位而不只靠「幣價為 0」**：畫面目前把 0 當「幣價缺值」（不顯示數字、改寫「會消耗遊戲幣」），需要一個明確的訊號才能顯示「不消耗遊戲幣」；明確的布林也比推論穩。
- **為什麼幣價仍回 0 而不是照實回設定值**：畫面所有「餘額不足」「幣價」的判斷都以幣價為準，回 0 讓即使舊版畫面（沒有讀 `coin_exempt`）也不會對管理員擋或提示；新欄位只負責文字。
- 欄位是新增，一般玩家的回應多一個 `false`，向下相容。

### D4：畫面只讀 `coin_exempt`，不自行判斷角色

`game-wallet.js` 讀取 `wallet.coin_exempt`（缺值視為 `false`，即維持一般玩家的行為）並暴露 `exempt`；`lowBalance` 在 `exempt` 時為假。`game-revision.js` 的 `revisionRegenerateNote`／`EditNote`／`RegenerateCostLine`／`EditCostLine` 多收一個 `exempt` 參數：免費次數用完且 `exempt` 時回「不消耗遊戲幣」（備註）與「這次重新生成／編輯不消耗遊戲幣」（Modal 說明），`editInsufficient` 為假。純文字渲染，不使用 `v-html`。

- 前端不看頁面上的角色或其他欄位，避免前端自行推論豁免；真正的豁免永遠在伺服器，畫面只是說明。

### D5：日誌與帳本

`wallet_events.log_hold` 照常記錄（金額 0）；不新增事件、不新增白名單欄位。帳本記錄頁對管理員會顯示金額為 0 的「冒險回合」列，可接受（營運自己的測試紀錄，反映真實發生的預扣）。個人資料匯出照常包含這些列。

## Risks / Trade-offs

- [管理員測試產生大量 0 幣帳本列] → 每輪一列（與一般玩家相同量級），記錄頁分頁；不另做清理。
- [豁免判斷的兩份定義日後分歧] → D2 讓 `AccessPolicy.is_admin` 委派同一個屬性，並加對照測試。
- [前端舊快取沒有 `coin_exempt`] → 缺值視為 `false`，且管理員的幣價回 0，舊畫面不會擋送出（最壞只是文字仍寫「會消耗」，下次載入即更新）。
- [管理員帳號被盜用可無限使用模型] → 全站 token 上限與管理員登入的雙重驗證（Google＋密碼）不變；`llm_call_logs` 仍記錄每次呼叫，營運告警（token 用量）照常適用。這是規範明確接受的取捨（營運測試需要）。
- [管理員的餘額仍可被「調整」] → 保留，供測試一般玩家的扣款路徑時用其他帳號；不在本次範圍。

## Migration Plan

沒有資料庫異動、沒有設定鍵、沒有 migration，不需要備份步驟。部署即生效；回復只需部署前一版程式（`coin_exempt` 是多出來的欄位，舊畫面忽略；已寫入的 0 幣帳本列對舊版程式無害）。

## Open Questions

- 無。簡報第 109、626、779、781 行與 RPG 回合圖的預扣說明是否補「管理員不扣幣」由使用者在 apply 時決定（見 proposal 的文件項）。
