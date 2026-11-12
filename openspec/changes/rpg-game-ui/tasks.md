# Tasks

> 批次建議（apply 前依 `openspec-batch-workflow` 請使用者確認）：
> ① 第 1 組（串流核心：SSE 解析、串流客戶端、狀態與錯誤對照；純邏輯）
> ② 第 2 組（本機暫存、同步與 API 封裝）
> ③ 第 3 與第 4 組合併（後端外殼、樣板與樣式，加遊戲掛載：session／wallet／`game.js` 與端到端整合測試；使用者決定合併，樣板與腳本同批完成，不留不一致的中間狀態）
> ④ 第 5 組（記事簿面板與 `/game` 預設分頁）
> ⑤ 第 6 組（瀏覽器實測、破壞驗證、全套測試與設計文件）
>
> 每批只跑相關測試，全套測試留到 6.3。批與批之間的相依：① → ② → ③；④ 依賴 ③；⑤ 最後。第 3、4 組內的任務順序不變（先樣板與約定，再依約定寫腳本），批內仍逐項跑相關測試。
> 測試約定：Node 測試以 `node --test "tests/js/**/*.test.js"`；pytest 用 `.venv\Scripts\python.exe -m pytest`，連真實 SQL Server `project_test` 的測試同時只能有一個 pytest 行程；背景跑全套時輸出存檔、不接 `tail`。Windows 主控台是 cp950，Node 與 pytest 的輸出導到 scratchpad 檔案再讀。
> 沒有遷移、沒有新環境變數、沒有新日誌事件；`app/` 內不得出現 Raw SQL（`tests/architecture/test_no_raw_sql.py`）。**不改任何後端端點**，只改頁面控制器的初始狀態。

## 1. 串流核心（純邏輯）

- [x] 1.1 新增 `app/static/js/game/sse-parser.js`（UMD 純函式）：`createParser()` 的 `feed(text)` 依空行（含 `\r\n\r\n`）切出 `{event, data}`，單行與多行 `data:` 皆可、`:` 開頭為註解、JSON 解析失敗或缺 `event` 的事件丟棄、不完整的尾端留在緩衝。驗證：新增 `tests/js/sse-parser.test.js` 通過——一塊含多事件、一個事件切成 1 字元一塊（含中文字串）、`\r\n` 結尾、註解行、未知事件名稱照樣回傳由呼叫端決定、壞 JSON 被丟棄而後續事件正常、尾端不完整時 `feed` 之後補齊。
- [x] 1.2 新增 `app/static/js/game/turn-stream.js`（UMD；`fetch`、計時器、`AbortController`、`TextDecoder` 皆由呼叫端注入）：`run({url, body, csrf, idleMs, totalMs, signal, onEvent})`，永不 reject，回傳 `{outcome, done, failure}`（`done`／`error_event`／`interrupted`／`timeout_idle`／`timeout_total`／`aborted`／`rejected`）；以 `TextDecoder({stream:true})` 解碼、任何位元組重設閒置計時器、總逾時不重設、外部 `signal` 與兩個計時器共用一個內部 `AbortController`、`finally` 清除計時器；帶 `X-CSRF-Token` 與 `credentials: 'same-origin'`；`!response.ok` 時讀 JSON 交給 `describeHttpFailure`（1.3）。驗證：新增 `tests/js/turn-stream.test.js`（假 `fetch` 回傳 `ReadableStream`，假計時器）通過——正常序列、`done` 前結束（含讀取例外）為 `interrupted`、`ping` 重設閒置、45 秒無位元組為 `timeout_idle`、180 秒為 `timeout_total`、外部取消為 `aborted`、`pagehide` 風格的 `signal` 中止、開始前 409／401／503 為 `rejected` 且帶 `failure`、切在多位元組字元中間的資料塊、**每個結果路徑計時器都被清除**、請求標頭與本文正確。
- [x] 1.3 新增 `app/static/js/game/game-state.js`（UMD 純函式，無請求、儲存、DOM）：時間軸資料模型與事件轉換（`meta`／`token`／`transition`／`state`／`done`／`error` 對應設計 D3 的表）、`mergeTurns(local, remote)`（以 `seq` 為準、後端覆寫）、`timelineFrom(opening, turns, inflight)`、`describeHttpFailure(status, body)`、`describeStreamFailure(result)`（逾時、中斷、取消、`error` 事件對照；提醒「可能已在伺服器完成」）、`describeNotebookFailure(info)`、`describeCheckinFailure(info)`、`noticeText(notice)`（年齡更正含更正前的值）、`notebookLength(text)`（NFC 後以 Unicode 字元計）、`canSend(state)`、`actionProblem(text, max)`。驗證：新增 `tests/js/game-state.test.js` 通過——每個函式的正常與邊界案例；錯誤對照涵蓋規格列出的每個狀態碼與代碼（400、401、403 數種、404、409 的 `insufficient_coins`／`turn_in_progress`／`opening_in_progress`／`session_ended`／`notebook_conflict`／`checkin_overflow`／`already_checked_in`、422 兩種、429、503）；**錯誤訊息不含任何傳入的玩家文字**（帶標記字串的輸入斷言）；`notebookLength` 與後端 `notebook_policy.length_of` 的取樣案例（含組合字、表情符號、控制字元）逐項一致（取樣值寫死在測試中，並以一個 pytest 比對後端結果）；被擋與中斷的回合不含任何文字；`state` 事件只取白名單欄位。
- [x] 1.4 `game-labels.js` 補串流結束狀態、轉場與中斷說明、記事簿與簽到用的繁體中文對照（未知值退回固定文字）。驗證：`tests/js/game-labels.test.js` 擴充通過，既有案例不改。
- [x] 1.5 同步文件：`docs/前端結構-frontend.md`（新增「遊戲主畫面」小節與串流資料流圖，列出新模組與 Node 測試）。驗證：`tests/test_design_docs.py` 通過；`node --test "tests/js/**/*.test.js"` 通過（本組的筆數與基準 446 比較）。

## 2. 本機暫存、同步與 API 封裝

- [x] 2.1 新增 `app/static/js/game/game-store.js`（UMD，IndexedDB，失敗退回記憶體；**不修改 `cs-store.js`**）：資料庫 `game-cache-<public_id>`，集合 `sessions`、`turns`（key＝`<存檔>|<補零序號>`）、`inflight`（每存檔一筆）；API：`openStore`、`putSession`／`getSession`、`putTurns`／`turnsOf`／`maxSeq`、`setInflight`／`getInflight`／`clearInflight`、`clearSession`。被擋與中斷的回合只存序號與狀態；不存記事簿全文。驗證：新增 `tests/js/game-store.test.js` 通過（Node 內以假 IndexedDB 與記憶體退路兩種後端）——寫入與讀取、同序號覆寫、`maxSeq`、每存檔最多一筆在途、兩個帳號名稱不同而互相看不到、IndexedDB 開啟失敗與寫入配額錯誤時退回記憶體且不拋出、`clearSession` 清掉該存檔的全部集合、**檢查整個資料庫內容沒有記事簿全文、email、生日與年齡區間的欄位**。
- [x] 2.2 新增 `app/static/js/game/game-sync.js`（UMD，`api` 與 `store` 注入）：`catchUp(sessionId)`（以本機最大 `seq` 做 `since_seq` 迴圈直到 `has_more` 為假、後端覆寫本機同序號、網路失敗不拋出並回傳 `offline`）、`resolveInflight(sessionId)`（補齊後若本機有該 `client_msg_id` 的 `complete` 回合則清除並回傳 `null`，否則回傳在途紀錄）、`remember(sessionId, snapshot)`。驗證：新增 `tests/js/game-sync.test.js` 通過——增量補齊（本機 40、後端 41～42）、分頁（`has_more`）、後端覆寫快取、被擋回合不留文字、離線回 `offline` 且快取不變、在途回合的三種結果（已完成移除、仍未完成保留、補齊失敗保留）、404 清除該存檔、**從不自動呼叫串流**。
- [x] 2.3 `game-api.js` 擴充（axios，`timeout` 與 `signal` 由呼叫端傳入，檔內沒有逾時數字）：`snapshot(id)`、`turns(id, sinceSeq)`、`wallet()`、`checkin(confirmOverflow)`、`notebook(id)`、`saveNotebook(id, text, expectedVersion)`；回應與失敗都經 `reject` 整理（附 `categories`）。驗證：`tests/js/game-api.test.js`（新增或擴充）通過——每個呼叫的方法、路徑、本文與 `signal`；沒有 PUT／DELETE 以外的寫入呼叫（寫入的呼叫恰好是 `checkin` 與 `saveNotebook`）；`tests/test_game_pages.py` 既有的「入口頁不含寫入呼叫」斷言依新契約有意識地更新（不是刪除）。
- [x] 2.4 同步文件：`docs/前端結構-frontend.md` 補本機暫存的資料集、同步流程與已知限制。驗證：`tests/test_design_docs.py` 通過；本組 Node 測試全數通過。

## 3. 後端外殼與遊戲頁樣板

- [x] 3.1 `app/controllers/game_page_controller.py`：`game_session_page` 仍呼叫 `GameEntryService.session_summary`（非擁有者與不存在同一個 404，權限判斷不寫在 Controller），改渲染遊戲頁並傳入初始狀態 `{session:{id,title,content_tier,ended,player_name}, user:{public_id,member_tier}, gate, stream:{idle_timeout_ms:45000,total_timeout_ms:180000}, limits:{action_max}}`（逾時為具名常數，`action_max` 引用 `game_service.ACTION_MAX`）；更新檔頭說明。驗證：`tests/test_game_pages.py` 的存檔頁案例改寫通過——自己的存檔 200 且初始狀態逐鍵符合、**初始狀態不含回合內容、第 0 幕、記事簿、世界狀態、email、生日與年齡**、兩個逾時與上限數值固定、別人的與不存在的逐字相同 404、未登入導向登入（帶返回位置）、被鎖定導向 `/appeal`、`/game/start/<x>` 仍是精靈、管理員未確認年齡時 `gate` 為 `age_confirmation_required`。（原本「劇情畫面建置中」「頁面不呼叫端點」的斷言是**刻意改變**，改寫時在測試註解說明。）
- [x] 3.2 `app/templates/game/session.html` 整頁取代：頂列（標題、內容模式、餘額、回到冒險）、時間軸區、輸入區、側欄（預設收合，頂列有開關按鈕與收合時的地點摘要；「世界」分頁；「記事簿」分頁留到第 5 組）、遊戲幣 Modal、離開與放棄在途回合的確認 Modal、Toast 區；樣板標頭列出 `game.js` 的 `setup()` 必須回傳的名稱約定（比照 `start.html`）；所有玩家與作者文字以 `{{ }}` 或 Vue 文字插值純文字渲染，沒有 `v-html`／`|safe`／`data-bs-*`；腳本載入順序依 D6。驗證：`tests/test_game_pages.py` 加樣板守門（資源都用 `url_for`、載入順序固定、沒有 `v-html`／`data-bs-*`／CDN、標籤成對、`lang="zh-Hant-TW"`、標題以插值輸出且對含 `<b>` 的標題顯示原文）；新增 `tests/js/game-template.test.js`（以 Vue 編譯器編譯樣板、檢查「樣板用到的名稱＝約定清單」）通過；`tests/architecture/test_frontend_rules.py` 通過。
- [x] 3.3 `app/static/css/game.css` 以 `gm-` 前綴擴充：兩欄版面（≥992px 側欄 320px，開啟時推擠主區域，**預設收合**）、手機覆蓋側欄與遮罩、時間軸氣泡（玩家行動／AI 回應／轉場／通知）、固定底部輸入區、「回到最新」、已完結與離線橫幅；深色琥珀配色、不使用純白、360px 不橫向溢出。驗證：`tests/test_theme_css.py`、`tests/test_fonts_and_static.py` 通過；新增 CSS 守門案例（沒有 `#fff`、沒有固定寬度造成溢出的規則、斷點為 992px）。
- [x] 3.4 同步文件：`docs/系統架構-architecture.md`（頁面控制器列與初始狀態）、`docs/前端結構-frontend.md`（樣板與載入順序）。驗證：`tests/test_design_docs.py`、`tests/test_readme.py` 通過；`tests/test_restricted_session.py` 的全路由掃描通過（路由數量不變）。

## 4. 遊戲掛載與整合

- [x] 4.1 新增 `app/static/js/game/game-wallet.js`（`createWallet(deps)`，用 Vue 全域的 `ref`／`computed`）：載入錢包、`done.coins` 覆寫餘額、`turnCost`、送出前不足的提示（不阻止送出）、`insufficient_coins` Modal 的開關、簽到（預覽與「取消／領取」、`already_checked_in`、`finally` 恢復、重複點擊只送一次）。驗證：新增 `tests/js/game-wallet.test.js`（`vm` 沙盒與替身 api）通過——規格 `rpg-wallet-ui` 的每個情境，含錢包讀取失敗顯示「—」、取消不送第二次請求、領取帶 `confirm_overflow: true`、簽到成功不自動重送行動。
- [x] 4.2 新增 `app/static/js/game/game-session.js`（`createSession(deps)`）：載入順序（快照 → 第 0 幕 → 補齊 → 並行錢包）、離線與恢復連線、第 0 幕三種狀態與 `opening_in_progress` 的等待（5 秒、最多 24 次）、送出（新 `client_msg_id`、先存在途回合、`run`、`finally` 恢復並保留失敗時的行動文字）、事件到時間軸的轉換與每氣泡一個打字機、`transition`／中斷／`error` 的處理、`done` 後的補齊與在途清除、在途回合的「重送／放棄」、取消與 `pagehide` 中止、已完結與閘門的禁用、世界狀態與通知、時間軸只渲染最近 60 項與「顯示更早的內容」、「回到最新」的捲動判斷。驗證：新增 `tests/js/game-session.test.js`（`vm` 沙盒，替身 `fetch` 串流、替身打字機計時器、替身 store）通過——規格 `rpg-game-screen`、`rpg-stream-client`、`rpg-game-sync` 的每個情境：已 `ready` 的第 0 幕不呼叫開場串流、`pending` 時呼叫且 `done` 後解鎖、正常一輪、空白與超長行動不送出、失敗保留行動、重複點擊只送一次、`done` 前斷線視為中斷、自動重生成功與兩次被擋、重送重播不扣幣、重新整理後補齊移除在途、離線可讀不可送、404 清快取、`pagehide` 中止、**攔截 `console` 斷言整個流程沒有玩家文字被輸出**、**儲存物件斷言沒有 `localStorage`／`sessionStorage` 寫入**。
- [x] 4.3 新增 `app/static/js/game/game.js`（Vue 掛載與接線，唯一碰 `document`／`window` 的檔案）並在 `session.html` 加入腳本；側欄開關（桌面與手機都預設收合；桌面推擠、手機覆蓋＋遮罩＋Esc 關閉；不記住狀態、不寫任何瀏覽器儲存）、頂列、Toast 與 Modal 接線、`UI.install`。驗證：新增 `tests/js/game.test.js` 側欄案例（預設收合、開關按鈕切換、桌面與手機寬度各自的推擠與覆蓋狀態、Esc 與遮罩關閉、**沙盒儲存物件斷言開關沒有任何寫入**）通過；`tests/js/game-template.test.js` 擴充為「`setup()` 實際回傳了約定的每個名稱」（沙盒做法同 `wizard.test.js`，不另起一份清單）；`tests/test_game_pages.py` 的載入順序案例含 `game.js` 在最後；`tests/architecture/test_frontend_rules.py` 通過。
- [x] 4.4 後端對前端契約的端到端整合測試：新增 `tests/test_game_screen_flow.py`（假 RPG 供應商，沿用 `test_rpg_flow`／`rpg_acceptance_harness` 的夾具；SQLite 與 `project_test`）——玩家開存檔 → `GET /game/<id>` 200 → 依前端的載入順序呼叫快照、第 0 幕串流、`turns?since_seq=0`、`GET /api/wallet` → 送一輪並以與前端相同的解析規則（測試內用 Python 版 SSE 解析）驗證事件順序與 `done.coins` 等於 `GET /api/wallet` 的合計 → 以相同 `client_msg_id` 重送得到 `replayed` 且餘額不變 → 補齊 `since_seq` 只回新回合 → 另一位玩家開同一網址得到 404 → 全程日誌掃描沒有行動、劇情、第 0 幕與角色卡。驗證：測試通過；`tests/test_log_scan_full_flow.py`、`tests/test_log_scan_protagonist.py` 通過。
- [x] 4.5 導覽：遊戲頁頂列的「回到冒險」「客服」「帳號」連結（一般站內連結，無 `data-bs-*`）；`/game/start/<id>` 成功後仍導向 `/game/<存檔>`（不改）。驗證：`tests/test_game_pages.py` 加連結案例；`tests/test_game_entry_flow.py` 通過（開存檔後 `/game/<id>` 是遊戲頁，不再有「建置中」）。
- [x] 4.6 同步文件：`README.md`（`/game/<id>` 頁面說明、前端模組、已知限制〔IndexedDB 在裝置上不隨帳號刪除清除〕、驗收紀錄骨架：6.1 至 6.3 如實寫「未驗證」）、`docs/前端結構-frontend.md`、`docs/系統架構-architecture.md`。驗證：`tests/test_readme.py`、`tests/test_design_docs.py` 通過；本組全部 Node 與 pytest 相關測試通過。

## 5. 記事簿面板與 `/game` 預設分頁

- [x] 5.1 新增 `app/static/js/game/game-notebook.js`（`createNotebook(deps)`）：開啟面板才 `GET …/notebook`、顯示全文與用量與預備空間、空狀態、已滿提示（面板與入口，用快照的摘要判斷）、編輯模式與草稿、即時字數與超過容量禁用儲存、`PUT` 帶 `expected_version`、`finally` 恢復、`notebook_conflict` 更新為後端最新並保留草稿、422／409／404／401／網路錯誤對照、未儲存離開的確認 Modal、已完結唯讀、不自動儲存。`session.html` 加「記事簿」分頁的樣板（`white-space: pre-wrap` 純文字、沒有 `v-html`），`game.js` 接線，`game-template` 的約定清單同步擴充。驗證：新增 `tests/js/game-notebook.test.js`（`vm` 沙盒）通過——規格 `rpg-notebook-panel` 的每個情境，含全文含 `<img onerror=…>` 字樣時以原文顯示、剛好用完容量不顯示已滿、2240／2000 顯示已滿與須一次刪到容量以內、衝突後草稿仍可取得、422 只顯示類別、重複點擊只送一次、**記事簿全文不進 IndexedDB 與主控台**；`tests/js/game-template.test.js` 與 `tests/test_game_pages.py` 的樣板案例通過。
- [x] 5.2 `app/static/js/game/entry.js`：掛載時並行載入「選劇本」與「我的存檔」第一頁；存檔非空且玩家尚未手動切換時切到「我的存檔」，存檔請求失敗維持「選劇本」。驗證：`tests/js/entry.test.js` 擴充通過——沒有存檔仍為「選劇本」、有存檔預設「我的存檔」、手動切換後載入完成不被覆蓋、存檔請求失敗退回並可重試、既有案例無須修改即通過（預設分頁那一項有意識地改寫，並在測試註解說明）；`tests/test_game_pages.py` 通過。
- [x] 5.3 同步文件：`docs/前端結構-frontend.md`、`README.md`（記事簿面板、預設分頁），`tests/test_readme.py` 守門。驗證：`tests/test_design_docs.py`、`tests/test_readme.py` 通過。

## 6. 整合與收尾

- [ ] 6.1 在瀏覽器實際操作一次（驗收環境 `..\newprojet-acceptance\start-acceptance.ps1`，**不要加 `-Reset`**，埠避開 5000；假 RPG 模型用行動文字的關鍵字觸發情境）：①開始冒險後直接進遊戲畫面、第 0 幕逐字出現、重新整理不重抽；②送一輪：打字機、餘額 −30、世界狀態更新；③`測試被擋一次`（轉場文字後新氣泡）、`測試被擋兩次`（轉場後解鎖並提示換個行動）、`測試中斷`（中斷說明後補齊）、`測試慢速`（不誤判逾時）、`測試結局`（已完結、輸入禁用）；④餘額不足 Modal → 簽到（含超過上限的預覽）；⑤記事簿：檢視、用量、`測試記事簿超量` 的已滿提示、編輯儲存、另一分頁造成的版本衝突；⑥重新整理與離線（開發者工具切離線）後歷史仍可閱讀、送出被禁用；⑦重新整理中途串流後的在途回合提示；⑧`/game` 有存檔時預設「我的存檔」；⑨側欄在桌面與手機都預設收合、按開關可開（桌面推擠、360px 覆蓋＋遮罩）、重新整理後回到收合，360px 寬度下不橫向溢出；⑩開發者工具的儲存區沒有記事簿全文。驗證：把觀察結果（文字描述，能截圖則附路徑）記入 README 驗收紀錄；做不到的項目明講「未驗證」（AI 生成預覽沿用 rpg-entry-ui 的「未驗證」）。
- [ ] 6.2 破壞驗證（每項驗證後以 `git checkout`／備份還原，並位元比對）：①`turn-stream.js` 在 `done` 之前結束仍視為完成 → 串流與場景測試失敗；②`ping` 不重設閒置計時器 → 逾時測試失敗；③`transition` 不丟棄已顯示的氣泡 → 被擋回合測試失敗；④補齊不帶 `since_seq`（永遠從 0）或不覆寫快取 → 同步測試失敗；⑤`done` 後不清除在途回合 → 重新整理情境測試失敗；⑥快取寫入記事簿全文 → 儲存內容守門失敗；⑦簽到預覽未經確認就帶 `confirm_overflow: true` → 錢包測試失敗；⑧時間軸樣板改用 `v-html` → 樣板守門與架構測試失敗；⑨控制器的初始狀態塞進第 0 幕或劇情 → 初始狀態測試失敗；⑩`pagehide` 不中止請求 → 場景測試失敗。驗證：十項都至少有一個測試失敗，結果記入 README 驗收紀錄；若某項沒有測試失敗，先補強測試再重跑。
- [ ] 6.3 全套驗證：`pytest -q`（SQL Server 服務 `MSSQLSERVER` 執行中、同時只能一個 pytest 行程、輸出導到檔案、跑的期間不改任何被測試讀取的檔案）、`node --test "tests/js/**/*.test.js"`、`openspec validate rpg-game-ui --strict`。驗證：全部通過，筆數記入 README 驗收紀錄；有失敗先修再重跑（若失敗無法重現，如實記錄，不當成通過的依據）。
- [ ] 6.4 更新設計文件並讓測試通過：`docs/RPG.md` 7.17（新增 7.17.7「`rpg-game-ui`（RC-3b）的實作結果」：串流客戶端的決定、IndexedDB 的角色、簽到入口、預設分頁、與 RC-2c 的銜接）、`docs/路線圖-roadmap.md`（`rpg-game-ui` 節點與狀態表、日期）、`docs/README.md` 對照表如有需要。驗證：`tests/test_design_docs.py`、`tests/test_readme.py`、`tests/test_agents_skill_sync.py`、`tests/architecture` 通過。

## Workflow follow-up

- 使用者驗收（含 6.1 的瀏覽器操作）後，新對話 `/openspec-archive-change rpg-game-ui`（封存前先同步 delta 到主規格：新增 `rpg-game-screen`、`rpg-stream-client`、`rpg-game-sync`、`rpg-wallet-ui`、`rpg-notebook-panel`，修改 `rpg-entry-pages`；封存後更新路線圖的狀態與日期，並複查各設計文件標頭的「對應 change」）。
- 之後是 RC-2c（玩家重新生成與編輯輸入；它的按鈕掛在本 change 的時間軸最後一輪上）與 `onboarding-polish`。
- 沒有部署前置步驟（無遷移、無新設定）。
