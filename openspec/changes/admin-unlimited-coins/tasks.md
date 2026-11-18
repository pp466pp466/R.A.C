# Tasks

> 批次（使用者 2026-11-18 已確認）：① 第 1、2 組合成一批（後端：豁免判斷、錢包服務、遊戲服務與其 pytest）　② 第 3 組（前端）　③ 第 4 組（文件、驗收、全套測試，最後做簡報任務 4.5，簡報與圖解單獨一個提交）。瀏覽器實測 4.3 由使用者親自做。
> 測試約定：pytest 用 `.venv\Scripts\python.exe -m pytest`，連真實 SQL Server `project_test` 的測試同時只能有一個 pytest 行程；背景跑全套時輸出存檔、不接 `tail`；Node 測試以 `node --test "tests/js/**/*.test.js"`，Windows 主控台是 cp950，輸出導到 scratchpad 檔案再讀。
> 約定：`app/` 內不得出現 Raw SQL（`tests/architecture/test_no_raw_sql.py`）；日誌只記內部編號、列舉與整數；改檔後用 Python 數 lone-CR（LF／CRLF 混用）；Bash 命令裡的雙反斜線會被折成單個，寫檔用 Write／Edit。
> 本 change 沒有 migration、資料表或設定鍵異動，不需要更新 `docs/project資料結構文件.md`。AGENTS.md 已於 `9232f18` 修訂，不需再改。

## 1. 豁免判斷與錢包服務

- [x] 1.1 `app/services/current_user.py`：`CurrentUser` 新增屬性 `is_active_admin`（`role == ROLE_ADMIN and status == STATUS_ACTIVE`，附一行說明：遊戲幣豁免與後台權限共用這個定義）；`app/services/access_policy.py` 的 `AccessPolicy.is_admin` 改為委派 `user.is_active_admin`。驗證：新增測試以「角色（user、admin）×狀態（active、suspended、deletion_pending）×會員等級（free、plus、vip）」窮舉，斷言 `is_active_admin` 與 `AccessPolicy.is_admin` 結果相同，且只有「admin＋active」為真、與會員等級無關；既有 `tests/test_access_policy*.py` 與後台權限測試通過。
- [x] 1.2 `app/services/wallet_service.py`：`hold` 在驗證鍵與存檔識別碼、讀取幣價並 `_prepare` 之後，若 `current.is_active_admin` 則 `plan` 回傳「兩個餘額不變、增減量 0」的 `turn_hold` 草稿（保留 `session_public_id`、不檢查餘額、不呼叫 `log_insufficient`），回傳 `Hold.cost` 為 0；非管理員路徑完全不變。更新檔頭與 `hold` 的 docstring（管理員豁免、金額為 0 的帳本列）。驗證：`tests/test_wallet_spend.py` 擴充——餘額 0 的管理員預扣成功、餘額仍 0、有一筆兩個增減量皆 0 的 `turn_hold` 列；有餘額（免費 100、付費 50）的管理員預扣並結算後餘額不變；同一冪等鍵重送只有一筆且 `existed=True`；預扣後退回再結算被拒（`HoldAlreadyRefunded`），結算後退回被拒（`HoldAlreadySettled`）；逾時的管理員預扣被 `_release_stale` 退回且餘額不變；一般玩家（含 vip）餘額 0 時仍拋 `InsufficientCoins`；被停權（非啟用）的管理員不豁免。
- [x] 1.3 `app/services/wallet_service.py`：`WalletView` 新增 `coin_exempt: bool` 並輸出於 `to_dict`；`_view` 對管理員把 `turn_cost` 各鍵設為 0、`coin_exempt` 為真，其餘使用者維持設定值與假。驗證：`tests/test_wallet_service_core.py` 與 `tests/test_wallet_api.py` 擴充——管理員查詢錢包得到 `coin_exempt: true`、`turn_cost.standard` 為 0、餘額照實且沒有被發放任何幣；一般玩家（free、vip）為 `false` 與設定值；回應沒有內部整數編號（沿用既有案例）；請求帶任何 `coin_exempt`、`admin` 之類的查詢參數、標頭或本文都不改變結果。

## 2. 遊戲服務：回合與修訂

- [x] 2.1 `app/services/game_service.py`：`_last_turn_view` 對 `current.is_active_admin` 的 `regenerate_cost` 與 `edit_cost` 一律為 0（`free_left` 照實）；確認 `begin_turn`、`begin_revision` 不需要其他改動（兩者都經 `wallet.hold`，免費次數的判斷與認領不變），並在 `begin_revision` 的 docstring 補一句管理員的預扣為 0。驗證：`tests/test_game_service_begin.py` 與 `tests/test_game_service_revision.py` 擴充——餘額 0 的管理員 `begin_turn` 成功（沒有 `InsufficientCoins`）且 `TurnPlan.cost` 為 0；管理員對同一回合第 3 次修訂（重新生成與編輯各一個案例）成功，認領與回饋列存在、帳本有金額 0 的預扣、`regen_count` 計次與一般玩家相同；同樣情境的一般玩家（餘額 0）仍回 `InsufficientCoins` 且不留認領與回饋列；`last_turn` 的兩個費用對管理員為 0、對一般玩家在免費用完後為幣價。
- [x] 2.2 端到端：`tests/test_rpg_flow.py`（或 `test_rpg_revision_flow.py`）新增管理員案例，走完整的回合串流與付費修訂串流（餘額 0）：`done.coins` 為 0、帳本預扣列金額 0 且已結算；中斷後重送使用新的 `attempt` 與新的鍵（`attempt` 加 1）、仍是 0 幣；被擋回合退回的預扣（金額 0）使帳本的退回列也為 0；`llm_call_logs` 照常有模型呼叫紀錄。驗證：上述案例通過，且現有的一般玩家流程測試不變。
- [x] 2.3 管理員仍受全站 token 上限約束：新增或擴充案例，在全站每日 token 已達上限時管理員送出行動回 `system_busy`（與一般玩家相同）。驗證：案例通過。

## 3. 前端

- [x] 3.1 `app/static/js/game/game-state.js`／`game-api.js`：錢包回應的正規化加入 `coin_exempt`（只接受布林，缺值或型別不對視為 `false`）；`last_turn` 的費用維持「0 與正整數都是有效值」。更新 `game-api.js` 檔頭的錢包回應格式註解。驗證：`tests/js/game-state.test.js`、`tests/js/game-api.test.js` 擴充——`coin_exempt` 為 `true`／`false`／缺值／字串／數字的各種輸入。
- [x] 3.2 `app/static/js/game/game-wallet.js`：暴露 `exempt`（來自錢包回應，載入失敗時為 `false`）；`lowBalance` 與 `lowBalanceText` 在 `exempt` 時為假與空字串；`turnCost` 的處理不變（0 仍視為缺值）。驗證：`tests/js/game-wallet.test.js` 擴充——`coin_exempt: true` 且餘額 0、幣價 0 時沒有餘額不足提示；一般玩家餘額低於幣價仍有提示；錢包載入失敗時維持原本行為。
- [x] 3.3 `app/static/js/game/game-labels.js` 與 `game-revision.js`：`revisionRegenerateNote`、`revisionEditNote`、`revisionRegenerateCostLine`、`revisionEditCostLine` 多收一個 `exempt` 參數（放在最後，缺值視為假，維持既有呼叫相容）：免費次數用完且 `exempt` 時，備註回「不消耗遊戲幣」，重新生成 Modal 說明「這次重新生成不消耗遊戲幣」，編輯 Modal 說明「這次編輯不消耗遊戲幣」，不提餘額與簽到；`editInsufficient` 在 `exempt` 時為假。還有免費次數時的文字不變。全部純文字渲染，不使用 `v-html`。驗證：`tests/js/game-labels.test.js`、`tests/js/game-revision.test.js`、`tests/js/game.test.js` 擴充——管理員免費次數內、用完、餘額 0 三種組合的文字與按鈕狀態；一般玩家的既有案例不變；`tests/test_game_template.py`（或既有的樣板測試）通過，確認沒有新增 `v-html` 與 `data-bs-*`。

## 4. 文件、驗收與全套測試

- [ ] 4.1 `docs/RPG.md`：遊戲幣一節（7.7）與修訂費用表（7.8 第 9 列附近）補「管理員帳號不消耗遊戲幣：預扣寫入金額 0 的帳本列、免費次數照常計數、不因餘額不足被拒；客服額度與全站 token 上限不受影響」，並在 RA 或變更紀錄處註明規範來源（AGENTS.md，`9232f18`）；`docs/前端結構-frontend.md` 的錢包與修訂模組補 `coin_exempt` 的讀取與文字；其他受影響的設計文件依 `docs/README.md` 的對照表檢查（`docs/系統架構-architecture.md` 的服務層若有提到預扣，補管理員例外），更新各文件的「最後更新」與「對應 change」。驗證：`tests/test_design_docs.py` 通過。
- [ ] 4.2 驗收腳本：在 `scripts/acceptance_rpg_turn.py`（或新增小腳本，沿用其建立帳號與假模型的做法）加入管理員案例——餘額 0 的管理員跑一輪與兩次以上的修訂，驗證回合完成、帳本金額 0、餘額仍 0、`/api/wallet` 回 `coin_exempt: true`；同一腳本中餘額 0 的一般玩家仍得到 409 `insufficient_coins`。`tests/test_acceptance_rpg_scripts.py` 補對應的結構測試。驗證：驗收腳本在驗收環境（`newprojet-acceptance`，不要動使用者的 5000 埠）通過；腳本測試通過；腳本輸出不含玩家文字。
- [ ] 4.3 瀏覽器實測（請使用者親自做，或以 Playwright 經 Chrome channel 輔助）：管理員帳號登入（Google＋密碼雙步驟）開一個存檔，餘額為 0 時送出行動不被擋、頂列沒有餘額不足提示；修訂用完 2 次免費後，兩個按鈕旁顯示「不消耗遊戲幣」，編輯與重新生成的 Modal 沒有「會消耗遊戲幣」，送出可成功；換一般玩家帳號確認行為沒變（餘額不足仍有提示與 409 Modal）。修改 `templates` 後須請使用者重啟驗收伺服器（JS 直接生效）。驗證：結果記入 `README.md` 的驗收紀錄（admin-unlimited-coins）。
- [ ] 4.4 全套測試：`node --test "tests/js/**/*.test.js"` 與全套 pytest（背景執行、輸出存檔；SQL Server 測試同時只有一個行程）全部通過，包含 `tests/architecture`、`tests/test_design_docs.py`、`tests/test_agents_skill_sync.py`、`tests/test_readme.py`。驗證：結果摘要（通過數）寫入 `HANDOFF.md`。
- [ ] 4.5 簡報與圖解更新（最後一個任務；依 `deck-conflict-watch` 與 `HANDOFF.md` 的簡報待辦，簡報與圖解**單獨一個提交**，與程式、文件的提交分開）：①本 change 造成的衝突——`docs/2.0簡報.md` 第 109、626、779、781 行與 `docs/diagrams/build_diagrams_system.py` 的 RPG 回合圖（預扣與遊戲幣兩處）補「管理員不扣幣」；②adult-only-signup 已封存（`b4f07b6`）造成的 8 處「待封存」（第 20、33、49、57、67、697、915、927、928、972 行，細節見 `HANDOFF.md`）改為已封存，Mermaid 樣式 `:::active` → `:::done`，數字改為 26 個 change 已封存、主規格 87 個；③提交數與其他量測數字（模組、行數、測試案例）重新量測；④只有圖解產生器的文字改了才重產 PNG，最後重產 PDF（`mermaid@11.4.1`、`marked@12.0.2` 在 scratchpad `deck-libs`，PyMuPDF 在 `pylibs`，被清除要重裝）。實作前先把每一處的行號與擬改文字列給使用者確認。驗證：`build_deck.py --check` 通過、目視 PDF、`tests/test_design_docs.py` 通過；提交只含簡報、圖解與 PDF。

## Workflow follow-up

- 批次完成後更新 `HANDOFF.md`（不進 commit）；依使用者要求才 commit。
- 封存前確認設計文件已更新；封存後更新 `docs/路線圖-roadmap.md` 的狀態與日期，並複查各設計文件標頭的「對應 change」。
