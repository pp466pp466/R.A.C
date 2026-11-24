# Tasks

> 分批與測試節奏：apply 之前先依 `openspec-batch-workflow` 提分批方案並由使用者確認；每批只跑「相關測試加全域守門子集」（`tests/architecture`、`test_service_errors`、`test_design_docs`、`test_readme`、`test_restricted_session`、`test_legal_pages`、`test_agents_skill_sync`）。動到後端（`app/`）、端點、遷移、抹除或權限路徑的批次才加跑相關的後端測試；全套 pytest 只在第 8 組。

## 1. 規範修訂（須使用者確認文字後才寫）

- [x] 1.1 依 `design.md` D9 的草案修訂 `AGENTS.md` 第 5、6、7、10、11 節（最小改動），先把每一節的新舊對照給使用者確認；驗證：使用者確認，且 `git diff AGENTS.md` 只有草案列出的段落
- [x] 1.2 同步 `.claude/skills/python-flask-skill` 的對應 reference，並在 `tests/test_agents_skill_sync.py` 的 `SYNCED_RULES` 補守門句；驗證：`pytest tests/test_agents_skill_sync.py` 通過，且新增的守門句在 AGENTS.md 與 reference 都找得到

## 2. 後端：直接推進劇情的平台規則

- [x] 2.1 在 `rpg_prompt.py` 新增 `ADVANCE_ACTION = "推進劇情"` 與回合敘事專用的推進規則（只進回合敘事的系統指令，不進第 0 幕、狀態、記事簿與之後的建議）；驗證：新增的 `tests/test_rpg_advance_prompt.py` 斷言規則只出現在回合敘事、安全模式界線仍在、玩家文字不在系統指令
- [x] 2.2 確認「推進劇情」走一般回合的預扣、規則層、重新生成與編輯，不需特例；驗證：以假模型跑一輪 `action="推進劇情"`，斷言預扣 30 幣、`insufficient_coins` 路徑、重新生成可用、同一 `client_msg_id` 重送不重複扣幣（沿用既有回合測試的夾具）
- [x] 2.3 跑相關測試與全域守門子集；驗證：通過

## 3. 後端：資料結構、快照與匯出

- [x] 3.1 在 `RpgSession` 新增 `suggestions_json`（JSON、可為空），撰寫遷移 `0023`（expand，`recreate="never"` 的 downgrade），驗證：升降級測試與 `alembic check`（SQLite）通過；舊資料列讀出為空值
- [x] 3.2 在 `session_repository.py` 新增讀取目前錨點與保存建議的方法（所有方法帶 `user_id`；寫入不改 `version`；寫回前比對錨點的條件式更新）；驗證：repository 測試覆蓋錨點相等才可讀、錨點改變後不可讀、並行寫入只保留一份、他人存檔回無資料
- [x] 3.3 `session_snapshot` 帶出 `suggestions`（有效才有；不含錨點細節）；驗證：快照測試覆蓋新存檔、有效、完成新回合後過期、修訂成功後過期
- [x] 3.4 `personal_export_service` 的 `SESSION_COLUMNS` 與 JSON 匯出加入建議；驗證：匯出測試覆蓋有建議與沒有建議（JSON 字串陣列、CSV 換行連接、公式前綴規則照舊）
- [x] 3.5 確認存檔刪除與帳號抹除涵蓋新欄位（同一資料列，預期不需改程式）；驗證：既有的抹除測試加一項斷言「建議隨存檔消失」
- [x] 3.6 同步 `docs/project資料結構文件.md`（欄位表、第 14 節異動紀錄）並用既有的列骨架工具核對；驗證：`pytest tests/test_project_db_schema_doc.py` 通過
- [x] 3.7 跑相關測試與全域守門子集；驗證：通過

### 批 2 完成註記（2026-11-23；只跑相關測試加全域守門子集，**未跑全套 pytest 與 Node**，全套留到 4.9 與第 8 組）

- **2.1**：`ADVANCE_ACTION = "推進劇情"` 與 `_ADVANCE_RULE` 放在 `rpg_prompt.py`，串在 `_ACTION_RULE` 之後交給同一個 `action_rule` 參數，所以只進回合敘事（`build_system`）；第 0 幕、狀態、記事簿的系統指令都沒有（`tests/test_rpg_advance_prompt.py` 斷言）。建議呼叫的系統指令在批 3 才存在，那一批的測試要補斷言它也不含這條規則。前端常數與後端相等的斷言留到 6.1。
- **2.2**：確認不需要特例：`tests/test_rpg_advance_action.py` 以 HTTP 走完整流程（預扣 30 幣與帳本形狀和一般行動相同、餘額不足 409、重送冪等、重新生成與編輯共用免費次數、規則層照常擋）。
- **3.1**：欄位與遷移 `0023_story_suggestions`（檔名 `20261123_0023_story_suggestions.py`，downgrade 用 `recreate="never"`）；遷移測試在 `tests/test_migrations.py`（SQLite 升降級與再升級、兩軌的 JSON 寫入與讀回、`alembic check` 無差異）。
- **3.2（與 design D5 的出入）**：新增 `suggestion_state`（目前錨點與有效建議）與 `save_suggestions`（鎖內重新確認；回傳 `saved`｜`reused`｜`stale`｜`ended`，另有 `SuggestionState`、`SuggestionSave` 兩個結果型別）。**寫入的鎖是一次不改值的條件式 UPDATE（`version = version`，與記事簿抓取相同的做法）**，因為規範不允許依 JSON 內容篩選：錨點與「已有有效建議」的比對在鎖內以程式做。這次 UPDATE 會使 `updated_at`（資料表的 onupdate）前進，不改 `version`、世界狀態、記事簿、最後活動時間與回合列（測試明確排除 `updated_at` 並說明）。多一個 `ended` 結果（存檔在呼叫期間完結），設計文件的十個步驟沒有列。
- **3.3**：`session_snapshot` 的 `suggestions` 是字串陣列，過期或沒有為空陣列，不帶錨點。
- **3.4（解讀）**：匯出的是「資料庫裡目前保存的文字」，**不論錨點是否有效**（過期的建議仍存放在資料庫，屬個資，所以仍匯出）；JSON 為字串陣列，`sessions.csv` 為單一欄位以換行連接，公式前綴規則照舊。若要改成「只匯出有效的」，只需改 `export_sessions` 一處。
- **3.5**：沒有改程式；新增測試證明建議的文字隨帳號抹除與 365 天期限清除消失，且不影響別人的存檔、管理員沒有任何路徑（`tests/test_story_suggestions_storage.py`）。專案裡沒有「玩家自行刪除存檔」的路徑，存檔只經帳號抹除與期限清除消失。
- **3.6**：`docs/project資料結構文件.md` 已同步（標頭、第 1、2、5.18、7、9、12、14 節與 ERD；用 `python -X utf8 -m tests.support.db_schema sessions` 核對骨架，`sessions` 由 30 欄變 31 欄、全庫 269 欄；第 24 列新增、後面重新編號）；**尚未套用到實際的 `project`**，文件已如實寫明。
- **守門測試連帶的最小改動（不在原任務內）**：①`docs/路線圖-roadmap.md` 補上 `rpg-story-view`（立案提交時漏列，守門子集的 `test_the_roadmap_lists_every_archived_and_active_change` 本來就紅；已補表格一列、Mermaid 節點與一條邊）；②`docs/資料模型-data-model.md` 補最小的 0023 說明（標頭、ERD 欄位、遷移鏈、一條說明；`test_every_table_and_the_latest_migration_are_described_in_the_data_model_doc` 要求）。其餘設計文件（系統架構、前端結構、RPG.md 等）仍留到 9.2。

## 4. 後端：建議的提示詞、服務與端點

- [x] 4.1 新增純函式模組 `rpg_suggest_prompt.py`（固定系統指令樣板、`SUGGEST_SCHEMA`、`clean_items`、洩漏標記語意規則）並列入架構測試的純模組清單；驗證：單元測試覆蓋 NFC 與控制字元、長度 2 至 30、網址與電話與 email 樣式、去重、標記、至多 3 項、全部無效時回空
- [x] 4.2 在 `rpg_context.py` 新增 `build_suggest_context`（玩家可見世界、角色卡與來歷、最近 3 輪或第 0 幕；不含記事簿、NPC 切片、隱藏欄位）；驗證：測試以含隱藏特質與弧線階段的存檔組裝，斷言不出現；玩家文字只在使用者內容區；生效模式現算
- [x] 4.3 `llm_call_log.py` 新增 `KIND_RPG_SUGGEST`、加入 `CALL_KINDS` 與 `TRADITIONAL_KINDS`；驗證：既有的帳本與用途列舉測試更新後通過，且帳本不含文字
- [x] 4.4 新增 `SuggestionService`（前提檢查順序、重用、預算檢查、`run_structured`、規則層與安全模式過濾、寫回前再比對錨點、`suggestions_stale`）；驗證：服務測試覆蓋 `design.md` D5 的十個步驟，包含失敗不保存、重用不呼叫模型、不扣幣、備援模型、管理員相同行為
- [x] 4.5 在 `game_api_controller.py` 新增 `POST /api/game/sessions/<id>/suggestions`（JSON、CSRF、閘門）；驗證：端點測試覆蓋 404、`session_ended`、`opening_pending`、`turn_in_progress`、`system_busy`、沒有供應商、`suggestions_unavailable`、`suggestions_stale` 與成功回應的欄位；`/api/*` 回應不洩漏內部細節
- [x] 4.6 在 `rpg_events.py` 新增建議事件（白名單：內部編號、項數、是否重用、結果列舉）；驗證：日誌測試收集一輪建議流程，找不到任何建議、劇情或行動的文字
- [x] 4.7 確認呼叫的狀態：一個進行中（`streaming`）的回合而尚無完成回合時，錨點與 `turn_in_progress` 判斷正確；驗證：新增對應測試
- [x] 4.8 更新 `README.md` 的端點表與路由數，以及事件白名單、用途列舉等別處寫死的數量；驗證：`test_readme`、`test_design_docs` 與守門子集通過
- [x] 4.9 跑後端相關測試（`tests/test_rpg_*`、`tests/test_game_*`、帳本與遷移）與全域守門子集，並補跑一次全套 pytest（涵蓋第 3 與第 4 組；背景執行、輸出存檔）；驗證：全部通過並記錄筆數

### 批 3 完成註記（2026-11-23；後端：建議的提示詞、服務與端點；**補跑了一次全套 pytest**，沒有跑 Node 專屬的新測試——本批沒有前端）

- **4.1**：`app/services/rpg_suggest_prompt.py`（純函式，已列入 `tests/architecture/test_rpg_pure_modules.py`）：固定系統指令（沒有回合敘事的行動規則與推進規則）、`SUGGEST_SCHEMA`（最多 5 項，單項寬鬆上界 200 字）、`clean_items`（NFC、去控制與零寬字元、2 至 30 字、網址／信箱／電話樣式、去重、標記、設定用語與區塊標題、規則層、安全模式的性內容）、`suggest_rules`（洩漏標記；至少一個有效項）。網址／信箱／電話的判斷從 `nickname.py` 抽出共用的 `has_contact_pattern`（暱稱行為不變）。
- **4.2**：`rpg_context.build_suggest_context`（只有地點、局勢、目標、主角角色卡與來歷、最近 3 輪或第 0 幕；不帶記事簿、在場人物切片、劇本資料）。主角的錨定事實（地點、里程碑、人物三種）都帶。
- **4.3**：`KIND_RPG_SUGGEST = "rpg_suggest"`（11 字，放得進 `NVARCHAR(20)`，**沒有遷移**）、加入 `CALL_KINDS` 與 `TRADITIONAL_KINDS`（輸出簡轉繁）、後台標籤「行動建議」、`docs/project資料結構文件.md` 的兩處允許值與第 14 節紀錄。
- **4.4**：`app/services/rpg_suggest_service.py`（`SuggestionService`）。**與 design D5 的出入（我選的做法，可推翻）**：①`revision_state` 在「尚無完成回合」時沒有回合可判斷，所以新增 `SessionRepository.turn_in_progress`（未逾時的 `streaming` 回合或修訂認領，與 `start_turn`、`claim_revision` 拒絕並行的判斷相同），4.7 的情形由它判斷；②**重用先於「沒有供應商」與全站預算**（沒有模型呼叫就不需要它們；設計的順序把預算放在重用之後、沒有提到供應商）；③沒有供應商由 Service 判斷而不是 Controller 的 `_require_provider`，原因同②；④所有項目都無效時，語意規則把它當驗證失敗，所以會**以同一模型重試一次**才回 503（規格只寫「視為失敗」）；⑤**模型呼叫之後立刻 `release`（提交）**：請求在 4xx／5xx 時整個交易回復，而 `LlmService` 的最後一筆帳本只寫入、不提交，沒有這一步，失敗與 `suggestions_stale` 的呼叫會在回應之後從帳本消失（HTTP 與兩軌測試都驗證帳本留存）。
- **4.5**：`POST /api/game/sessions/<id>/suggestions`（沒有本文、回 `{suggestions, reused}`）；新增錯誤 `ActionSuggestionsUnavailable`（503 `suggestions_unavailable`）與 `ActionSuggestionsStale`（409 `suggestions_stale`），**刻意不叫 `SuggestionUnavailable`**（那是創角的 `suggestion_unavailable`，單數）。閘門與缺 CSRF 的參數化已把這個端點加進 `tests/test_game_api.py`。
- **4.6**：`rpg_events` 新增 `rpg.suggestion_served`（項數、`generated`｜`reused`、模型）與 `rpg.suggestion_failed`（八個原因列舉、呼叫次數）；事件由 22 個變 24 個。HTTP 測試掃整段日誌，找不到建議、劇情、行動、存檔識別碼與標記。
- **4.7**：`turn_in_progress` 的測試涵蓋：第一輪串流中而尚無完成回合、完成回合之後又有串流中的回合、逾時不再算、修訂認領中、他人與不存在回 `None`。
- **4.8**：README 端點表（`/api/game` 由 8 條變 9 條）、錯誤代碼、新章節「RPG 行動建議與推進劇情」與 `tests/test_readme.py` 的守門；設計文件（系統架構、前端結構、RPG.md、模型呼叫層等）仍留到 9.2。
- **4.9 與驗證**：新增測試 `tests/test_rpg_suggest_prompt.py`（72）、`tests/test_rpg_suggest_service.py`（39 項×SQLite 與 SQL Server 兩軌）、`tests/test_game_suggestions_api.py`（17）；修改 `test_rpg_events`、`test_service_errors`、`test_readme`、`test_zh_text`、`test_llm_call_log_repository`、`test_nickname`、`test_rpg_advance_prompt`（補斷言：建議的系統指令也不含推進規則）、`test_admin_format_labels`（自動涵蓋新標籤）。**全套 pytest（14:08 起、約 25 分鐘）：14995 通過、5 略過、2 失敗**——失敗是 `tests/test_game_screen_flow.py` 兩軌，硬寫的快照欄位集合少了批 2 新增的 `suggestions`（批 2 只跑相關測試與守門子集，所以當時沒有發現），已把它加進預期並補一項「新存檔的建議為空陣列」；修好後與 7.5 的樣式修正一併重跑相關測試、頁面測試與守門子集 **1468 通過、2 略過**，`node --test` **1610 通過**。**誠實的限制**：全套是在 7.5 的樣式修正與這項測試修正之前（或之中）開始的，所以「單一一次涵蓋最終樹的全套」還沒有；最後一批（8.2）的全套會補上。

## 5. 前端純函式：分頁與閱讀器

- [x] 5.1 新增 `story-pages.js`（UMD）：句界、引號深度、換行硬分界、3 句與 150 字上限、單句超長的切法、字素安全、`done` 與湊滿才釋出；驗證：`tests/js/story-pages.test.js` 涵蓋 `rpg-story-pages` 規格的分段與串流釋出情境，並有「串接還原原文」的性質測試（多組含標點、無標點、對話、表情符號的輸入）
- [x] 5.2 新增 `story-reader.js`（UMD，注入時鐘與打字機工廠）：`feed`、`next`、`prev`、`jumpToEnd`、`reset`、`restore`、等待狀態、`prefers-reduced-motion`；驗證：`tests/js/story-reader.test.js` 以假時鐘涵蓋打字中補完、翻頁、第一段停用 `‹`、回看直接完整顯示、串流等待、`done` 補完、被擋與中斷、修訂還原
- [x] 5.3 跑 Node 測試與全域守門子集（含前端架構守門）；驗證：通過，新檔沒有 CDN、`v-html`、Bootstrap JavaScript

### 批 4 完成註記（2026-11-23；前端純函式 `story-pages.js` 與 `story-reader.js`；只跑 Node 全套與全域守門子集，**未跑全套 pytest**——本批沒有動 `app/` 的 Python、端點、遷移或權限路徑）

- **5.1**：`app/static/js/game/story-pages.js`（UMD，`Game.paginate`、`PAGE_MAX_SENTENCES`、`PAGE_MAX_CHARS`）。`paginate(text, { done })` 回傳 `{ pages, pending }`。性質：**已釋出的段永不改變**（對任何前綴，釋出的段都是完整結果的前綴），串接還原原文（扣除分界處空白）。細節與規格的出入或補充（我選的做法，可推翻）：①**一個句子要等到後面出現下一個有內容的字元（或換行、或 `done`）才算完整**——規格情境寫「收到三個完整句、後續尚未到達 → 第一段釋出」，但「……」可能分成兩個片段到達，若句末標點一到就釋出，已釋出的段之後會被改寫；所以文字剛好停在第三句的句末標點時，要等下一個字元（實際上幾毫秒）或 `done` 才釋出；②只有標點沒有內容的「句」（例如開頭的「……」）不獨立成句，併入後面的句子；③引號深度同時認得「」『』與“”（規格只列前兩種；提示詞沒有規定模型用哪一種）；④超長句（超過 150 字）自己成為獨立的數段、不與鄰句合併（否則串流中的已釋出段可能被後到的內容改寫）；⑤長度以 UTF-16 單位計（中文等於字數），硬切只在字素邊界，單一字素超過上限時整個保留；⑥半形 `,` `;` 也算逗號分號。
- **5.2**：`app/static/js/game/story-reader.js`（UMD，`Game.createStoryReader`、`SPEAKER_YOU`、`SPEAKER_NARRATOR`）。動作：`reset`、`feed`、`next`、`prev`、`jumpToEnd`、`load`（載入既有回合：最後一段、不打字）、`discard`（被擋、轉場、中斷：只留一行說明、不留部分內容）、`snapshot`／`restore`（修訂失敗還原，只含基本型別、只在記憶體）、`destroy`；狀態以 `reader.state` 或 `onChange` 取得。打字機工廠與 `reducedMotion` 由外部注入（`UI.TypewriterBuffer` 即符合介面），時鐘以 `scheduler` 原樣轉交。`next()` 回傳 `completed`｜`advanced`｜`waiting`｜`end`｜`none`，供之後的接線判斷。**與 design D2 的出入**：多了 `load` 與 `discard`（D2 列的是 `feed/next/prev/jumpToEnd/reset/restore`），因為載入既有回合要避免先打字再取消、被擋與中斷要「不留部分內容」；`feed` 的第一個參數是**片段**（不是累計全文）。
- **5.3 與驗證**：新增 `tests/js/story-pages.test.js`（38 項，含五個性質測試：串接還原、段長上限、任何前綴的釋出結果是完整結果的前綴、任意切塊含切在代理對中間、無狀態）與 `tests/js/story-reader.test.js`（42 項，假時鐘加真正的 `UI.TypewriterBuffer`，另有手動控制的打字機證明過期回呼被忽略）。**`node --test` 全套 1690 通過**（原 1610）；pytest 全域守門子集加 `test_game_pages`、`test_script_page`、`test_v_cloak_rule`：**1242 通過、2 略過**（略過的是 `test_no_legacy_tier_checks` 既有的兩項）。前端規則守門（沒有 CDN、`v-html`、Bootstrap JavaScript、`console.log`、`eval(`、瀏覽器儲存）通過，且新檔另由 Node 測試掃描。**快速變異驗證**（暫存目錄的副本，14 個變異全被殺死；正式的變異驗證在 8.1）。**未驗證**：真正的瀏覽器行為（接線在第 6、7 組）、真實模型輸出的分段品質（只以人工字串與隨機字串驗證）。設計文件（前端結構等）與 README 的模組清單留到 9.2；兩支新檔尚未被任何樣板載入（7.1）。

## 6. 前端：狀態、API 與接線

- [x] 6.1 `game-state.js` 新增 `ADVANCE_ACTION`、劇情卡視圖、建議與推進的啟用條件與錯誤對照（含 `suggestions_unavailable`、`suggestions_stale`、`opening_pending`）；驗證：`tests/js/game-state*.test.js` 擴充後通過；新增斷言前後端的 `ADVANCE_ACTION` 相等
- [x] 6.2 `game-api.js` 新增建議端點呼叫（axios、逾時 35 秒、AbortController）；`game-store.js` 讀取快照的 `suggestions`，且確認建議不寫入 IndexedDB；驗證：Node 測試涵蓋請求、取消、重用，並斷言儲存層沒有建議
- [x] 6.3 `game-session.js` 把回合與第 0 幕的串流事件餵給 reader，載入既有存檔時 `jumpToEnd`，處理鍵盤翻頁（焦點與 Modal 判斷）、推進動作、建議動作（貼進輸入框並收回）與離開頁面中止；驗證：沙盒跑完整載入與一輪流程，攔截 `console` 後沒有玩家文字；涵蓋被擋轉場、中斷、重送、離線與已完結
- [x] 6.4 `game-revision.js` 改掛劇情卡：按鈕只在當前顯示最後一輪時出現；成功後從第一段重新閱讀、失敗還原舊內容與原本的段；驗證：`tests/js/game-revision*.test.js` 更新後通過，涵蓋 `rpg-revision-ui` 規格的情境
- [x] 6.5 跑 Node 測試與全域守門子集；驗證：通過

### 批 5 完成註記（2026-11-23；前端狀態、API 與接線；只跑 Node 全套與全域守門子集加頁面測試，**未跑全套 pytest**——本批沒有動 `app/` 的 Python）

- **6.1**：`game-state.js` 新增 `ADVANCE_ACTION`、`cardView`（閱讀器狀態 → 劇情卡欄位）、`suggestionItems`、`suggestBlocker`／`canSuggest`、`advanceBlocker`／`canAdvance`、`describeSuggestionFailure`；`sendBlocker` 多一個 `reading`（第 0 幕還沒翻到最後一段）。固定文字在 `game-labels.js`（`speakerLabel`、`storyText`、`suggestText`、`actionHint`）。前後端 `ADVANCE_ACTION` 相等由 `tests/js/game-state-story.test.js` 讀 `rpg_prompt.py` 原始碼比對。
- **6.2**：`game-api.js` 新增 `suggestActions(id, options)`（POST、無本文；**寫入的呼叫由五個變六個**，`tests/js/game-api.test.js` 與 `tests/test_game_pages.py` 已更新）。`game-store.js` 本來就是白名單，沒有改邏輯，只補註解；`tests/js/game-store-suggestions.test.js` 證明建議不進記憶體與 IndexedDB 兩種後端。
- **6.3**：`game-session.js` 以 `story-reader.js` 驅動劇情卡：`card`、`nextPage`、`prevPage`、`handleKey`、`advance`、`toggleSuggestions`／`pickSuggestion`／`closeSuggestions`（含 `suggestOpen`、`suggestItems`、`suggestBusy`、`suggestError`、`focusTick`）、`reading`、`suggestDisabled`／`suggestHint`、`advanceDisabled`／`advanceHint`。時間軸資料層（`items`、補齊、在途回合、IndexedDB）與舊的每氣泡打字機**都沒動**，閱讀器是並行加上去的。
- **6.4**：`game-revision.js` 的 `reviseKey` 與 `canStart` 加上「劇情卡當下顯示的就是最後一輪」；修訂開始時存下舊畫面並讓劇情卡從第一段重讀，失敗、被擋、被拒、中斷、取消時還原，結束時 `sync` 對齊後端。
- **出入與解讀（我選的做法，可推翻）**：①**`game.js`、`game-contract.js` 與 `session.html` 的新名稱接線留到 7.1**——樣板測試要求樣板用到的名稱與 `setup()` 回傳的名稱一一對應（沒用到就失敗），所以名稱只能與樣板一起加；本批 `game.js` 沒動，因此 **`deps.reducedMotion` 與 `deps.isModalOpen` 尚未注入、document 的 keydown 尚未接到 `handleKey`、`focusTick` 尚未有人監看**（7.1 一併做）；②`session.html` 只多載入 `story-pages.js` 與 `story-reader.js` 兩行（否則 `createSession` 在瀏覽器裡找不到閱讀器），畫面本身仍是舊的時間軸；③**第 0 幕「翻到最後一段才能行動」**（規格 `rpg-story-pages` 的情境）做成黏著旗標 `openingReached`：翻到過最後一段就不再因回看而鎖上；載入既有第 0 幕直接停在最後一段所以不鎖；④建議的前端逾時用頁面初始狀態的 `suggest_timeout_ms`，**會期頁目前沒有這個鍵，暫用 35 秒常數**（AGENTS.md 第 5 節）；後端要隨設定改變時，在 `game_page_controller.py` 的會期頁狀態補一行即可；⑤新一輪、第 0 幕串流與修訂開始時，已保存與已展開的建議一律清掉（伺服器會以錨點判斷，再按圖示只會得到 `reused`，不多花 token）；⑥被擋與中斷的那一輪，劇情卡維持一行說明（`readerHold`），直到後端出現它的完整內容、另一個裝置新增回合或下一次送出；⑦後端的第 0 幕串流一律以 `status: complete` 結束（`opening_state` 為 `ready` 或 `fallback`），所以第 0 幕被擋的情境以 `complete` + `fallback` 測試。
- **驗證**：`node --test` 全套 **1766 通過**（原 1690；新增 `game-state-story` 9、`game-story-session` 46、`game-story-revision` 16、`game-store-suggestions` 3、`game-api` 2）；pytest 全域守門子集加 `test_game_pages`、`test_script_page`、`test_v_cloak_rule`：**1242 通過、2 略過**；在 scratchpad 副本上快速變異 20 個全被殺死（正式變異在 8.1）。**未驗證**：瀏覽器實機行為（接線在 7.1）、真實 `UI.TypewriterBuffer` 與 `matchMedia` 的組合（測試用替身）。

## 7. 前端：版面與樣式

- [x] 7.1 改寫 `session.html`：劇情卡、說話者標籤、`‹`、等待狀態、行動區（建議圖示與 chips、直接推進劇情、輸入框）、歷史 Modal（`v-if`、純文字、載入更早內容）、桌機左側捷徑欄、氛圍背景層容器（`aria-hidden="true"`，內含 5 個空的流星裝飾元素 `gm-meteor`，見 design D11）；全部以 `{{ }}` 渲染，沒有 `v-html` 與 `data-bs-*`；驗證：頁面測試（`tests/test_game_page*.py`）更新後通過，架構守門通過
- [x] 7.2 `game.css`：氛圍背景層＝純 CSS 漸層底（專案色彩、無圖片、無純白）加**動態的流星效果**（design D11：5 顆空裝飾元素、`@keyframes` 只動 `transform` 與 `opacity`、頭 `#E2E4E9` 尾漸層到透明且少數帶 `#FF6B35` 暖色、週期約 6 至 14 秒並以負延遲錯開、位於劇情卡之下、`pointer-events: none`；未滿 992px 只留 3 顆；`prefers-reduced-motion` 時不顯示流星且無任何動畫）、劇情卡大字、桌機左欄與手機的按鈕位置、icon 與 chips 樣式（沿用既有 chips 規格）；驗證：新增樣式測試（`@keyframes` 只動 `transform` 與 `opacity`、`prefers-reduced-motion` 規則關閉動畫、背景與流星規則沒有 `url(` 與純白、`pointer-events: none`、手機只留 3 顆）通過；用無頭 Chrome 在 1280 與 360 寬度截圖逐張目視，並以間隔數秒的連續截圖確認流星確實移動，360px 沒有橫向捲軸、輸入區不遮住劇情卡；動態的觀感由使用者在 7.4 目視確認
- [x] 7.3 建議圖示使用內嵌 SVG 並含可及性標籤；劇情卡的完整文字另提供給輔助科技（打字中不重複朗讀）；流星容器對輔助科技隱藏；驗證：頁面測試斷言標籤與屬性存在（含流星容器的 `aria-hidden`）
- [x] 7.4 （2026-11-23 使用者改為：「你做功能檢查，功能無誤即可部署；我用實際帳號展示時直接使用，有問題再改」）跑 Node、頁面測試與全域守門子集；以假模型的自有實例（SQLite、假模型 9100、應用程式 5012）用無頭 Chrome 在 1280 與 360 寬度各操作一輪（翻頁、回看、歷史、推進、建議、重新生成、編輯、重新整理）並截圖；功能無誤後直接部署到展示環境；**動態流星的觀感與每段字數、翻頁手感由使用者在展示時目視，有問題再改**；驗證：測試通過、兩個寬度各 27／27 項檢查通過、部署 21 步通過
- [x] 7.5 修正（2026-11-23 展示環境發現）：遊戲主畫面載入較慢時露出原始的 `{{ }}` 範本文字（`{{ title }}`、所有 Modal 與提示）。原因：`game.css` 的 `.gm-shell { display: flex }` 與 `[v-cloak] { display: none }` 特異性相同、寫在後面，把遮蔽規則蓋掉；修法：`game`、`cs`、`admin`、`scripts`、`wallet` 五個樣式檔的規則一律改為 `display: none !important`（`auth.css` 本來就是）；7.1 改寫 `session.html` 時須保留根元素的 `v-cloak` 與這條規則；驗證：新增的 `tests/test_v_cloak_rule.py`（3 項）通過——每個樣式檔的遮蔽規則都有 `!important`、遊戲根元素確實自己設定了 `display`（所以必須有）、每個有 `v-cloak` 的範本都載入含遮蔽規則的樣式檔（含繼承的父範本）；**尚未部署到展示環境**（只改了樣式檔，但目前的工作樹已含批 2 與批 3，須隨遷移 `0023` 之後的下一次部署一起上線）

### 批 6 完成註記（2026-11-23；7.1–7.4）

- **7.1**：`session.html` 的主畫面改為劇情卡（說話者標籤、`‹` `›`、等待狀態、重新生成與編輯掛在卡內、建議圖示在卡右上、chips 在卡內）、快速操作列（「歷史」「推進劇情」；手機在卡下方，桌機 992px 以上在卡左側）、歷史 Modal（唯讀純文字、載入更早內容，沒有任何編輯按鈕）、`gm-sky` 氛圍層（`aria-hidden`、5 個空的 `gm-meteor`）；在途回合（重送、放棄）仍顯示在主畫面。時間軸資料層不動，完整時間軸只在歷史顯示。`game.js` 接線：`isModalOpen`、全頁 `keydown` → `session.handleKey`、`focusTick`（貼上建議後焦點進輸入框）、`openHistory`／`closeHistory`；移除 `jumpVisible`、`jumpToLatest`（規格明訂歷史沒有「回到最新」，session 仍保留該狀態、不影響行為）。契約檔與 `game-template.test.js` 的「修訂按鈕掛在哪裡」改為對照劇情卡；`test_game_pages.py` 的 Modal 清單加上 `HISTORY`。
- **7.2**：`game.css` 加劇情卡、chips、捷徑列、歷史捲動區與流星層（`@keyframes gm-meteor` 只動 `transform` 與 `opacity`；5 顆，週期 8、12、6、14、10 秒、負延遲錯開；第 2 顆帶 `#FF6B35`；位於劇情卡之下、`pointer-events: none`；未滿 992px 以 `not all and (min-width: 992px)` 只留 3 顆——既有測試只允許 992px 與 575.98px 兩個斷點，所以用這種寫法；`prefers-reduced-motion` 時不顯示）。寬度一律用 `inline-size` 或 `max-width`（既有測試禁止其他的 `width:` 宣告）。
- **7.3**：建議圖示是內嵌 SVG（`aria-hidden`）加按鈕的 `aria-label`、`aria-expanded`、`aria-controls`；劇情卡打字中的字 `aria-hidden`，打字結束才提供 `fullText` 給輔助科技；流星容器 `aria-hidden`。新增 `tests/test_game_story_page.py`（8 項）。
- **7.4 的驗證**：`node --test` **1766 通過**；pytest 守門子集加 `test_game_pages`、`test_game_story_page`、`test_script_page`、`test_v_cloak_rule`、`test_loadtest_harness`、`test_acceptance_rpg_scripts` **1301＋115 通過、2 略過**；**自有實例實機操作**（Chrome 無頭，1280 與 360 寬度各 **27／27** 項）：第 0 幕串流與第一段、點擊補完、第一段 `‹` 停用、輸入框打空白不翻頁、送出後先顯示「你」再翻到旁白、回合完成後送出鈕恢復、歷史含第 0 幕與行動且沒有修訂按鈕、建議產生並只貼進輸入框、推進劇情以固定文字送出、重新生成與編輯成功、重新整理後直接顯示最後一段、沒有橫向捲軸、沒有主控台錯誤；流星 12 秒內取樣出現 24 種狀態、最高不透明度 0.54（確實在動）。為此在 `scripts/rpg_acceptance_harness.py` 的假模型加了「行動建議員」分支（固定回 3 個建議）。
- **未驗證／限制**：假模型的劇情只有 1 至 3 句，所以**多段翻頁（3 句一段、回看第二與第三段）只有單元測試覆蓋，沒有實機**；真實模型的分段品質與第一段延遲、動態流星的觀感、手機實機的觸控手感，由使用者在展示時確認。**8.1 變異驗證、8.2 全套 pytest、9.2 設計文件仍未做**（使用者選擇先部署）。

## 8. 整體驗證

> 執行順序（使用者 2026-11-23 確認）：8.1 → 9.2（文件同步）→ 8.2 → 8.3 → 9.1，使全套測試看到的是最終的設計文件。

- [x] 8.1 變異驗證：對 `story-pages`、`story-reader`、`rpg_suggest_prompt.clean_items` 與 `SuggestionService` 的關鍵判斷（句界、湊滿釋出、錨點比對、規則層過濾、不扣幣）做變異，設定測試逾時與行程逾時；驗證：每個變異都被測試殺死，工作樹還原乾淨
- [x] 8.2 全套 pytest 與全套 Node 測試（背景執行、輸出存檔）；驗證：全部通過，記錄通過與略過的數量
- [x] 8.3 `openspec validate rpg-story-view --strict` 與 `alembic check`；驗證：兩者通過

### 批 7 完成註記（2026-11-23；8.1–8.3、9.1、9.2；**未提交**，等使用者下指令一起 commit）

> 執行順序：使用者 2026-11-23 改為「8 全做完再直接做 9」，所以實際順序是 8.1 → 9.2 → 8.2 → 8.3 → 9.1（8.3 的 `openspec validate` 與 `alembic check` 在 8.1 跑變異期間先跑過一次，全套測試之後以最終樹重跑）。

- **8.1 變異驗證**：腳本在 scratchpad 的專案副本上執行（專案原始碼未動；每個變異有 `node --test --test-timeout=15000` 或 pytest 的行程逾時，逾時算被抓到，結束時比對副本與專案逐檔相同），共 **211 個變異**：`story-pages.js` 62、`story-reader.js` 59、`rpg_suggest_prompt`（含 `clean_items`、`suggest_rules`、系統指令與結構描述）46、`SuggestionService` 28、`SessionRepository` 的建議錨點方法 16。第一輪 **164 個被殺死、47 個存活**；逐個以**差分模糊測試**（同一組隨機輸入餵給原版與變異版，句子層 6 萬組、閱讀器 3 萬組操作序列，Python 側以讀程式分析）分類：**28 個是測試缺口**（變異版行為真的不同、原本沒有測試分辨）、**19 個是等價變異**。
  - **缺口與補的測試**：新增 `tests/js/story-pages-edges.test.js`（句末標點每一種、『』與“”、巢狀引號的內層闔上、句後空白與收尾括號、字數剛好 150 與 151〔湊段與串流釋出兩處〕、含空白的字數、超長句逗號切法剛好 150、硬切的最後一塊留給後面的小句、連續逗號不讓一段以逗號開頭、逗號後的空白算前一小句）、`tests/js/story-reader-edges.test.js`（第 0 幕第一段的說話者是旁白、已結束或被丟棄的輪次忽略後續片段、**補完之後才到的打字機回呼不能改動畫面**〔既有測試在 `onUpdate` 後緊接 `onDone` 把問題蓋掉〕、說明狀態下 `jumpToEnd` 不動作也不通知、說明只算一頁、還原後仍等第一個劇情段）；Python：`test_rpg_suggest_prompt.py` 3 項（含大小寫的標記在項目過濾與語意規則中都被找到——原本的測試標記全是小寫英數，又被聯絡樣式〔長串數字〕先擋下而遮蔽；NFC 與大小寫去重要在 `limit=5` 下才看得出，原本被預設上限 3 遮蔽；結構描述最多 5 項）、`test_rpg_suggest_service.py` 4 項（存檔在請求中途消失的 404 兩處加保存時一處、上游放行沒有有效項時仍不保存）、`test_session_repository_suggestions.py` 1 項（`True == 1`：錨點恰為 (1, 1) 時存成布林的文件不能算有效；**ORM 以 `==` 判斷 JSON 有沒有變動，`{"seq": True}` 等於 `{"seq": 1}` 所以直接覆寫不會寫入，測試要先清成 None**）。
  - **補完後重跑那 47 個**：28 個全被殺死、19 個仍存活，**存活的正好是差分模糊測試分不出差異的那 19 個**，副本沒有殘留變異。
  - **19 個等價變異（不補測試，理由）**：`P38`（湊段不要求目前有句子）、`P43`（串流釋出不要求目前有句子）：目前沒有句子時 `flush()` 本來就不做事；`P45`（結案時不回傳空 pending）：呼叫端結案時 `tail` 一定是空字串；`P47`（`flush` 允許空白段）：句子必有內容，頁面不會是空的；`P50`（超長判斷 `>=`）與 `P53`（小句超長 `>=`）：剛好 150 字的句子與小句兩條路徑的結果相同；`P65`（硬切不要求目前有內容）：多出的空塊被 `splitLong` 最後的 `filter` 濾掉；`R08`、`R14`、`R16`（說明狀態下的 `prev`／`canPrev`／`canNext`）：有說明時索引一定是 0 或 −1（只有 `discard` 設說明，且清空所有段），可達的狀態下行為相同——只有人工構造的 `restore` 狀態才分得出來；`R20`、`R39`、`R41`（`awaitingFirst` 的冗餘判斷與寫入）：`awaitingFirst` 只在 `feed` 的「第一個劇情段接手」判斷裡被讀，而該判斷要求 `before === 0`；`awaitingFirst` 為假時一定已有劇情段（`before > 0`）或輪次已結束（`feed` 一開始就擋掉），所以 `R20` 拿掉它不改變可達狀態的行為，`R39`（已有段時不解除）與 `R41`（`load` 後仍等第一段，但 `load` 之後輪次已結束）也一樣；`R25`（沒有打字機視為不減少動態）：`createTypewriter` 不是函式時拋出，被 `try/catch` 接住後同樣直接顯示全文；`R32`（開始打字不遞增世代）：`startTyping` 只由 `showPage` 呼叫，而它先 `cancelTyping()` 已遞增；`R51`（`pageText` 不檢查上界）：可達狀態下索引不超過最後一頁；`R52`（已釋出的段變動時不更新）：`paginate` 的性質保證不會發生（防禦性程式碼）；`R59`（說明時 `typing` 仍為真）：進入說明狀態一定先 `cancelTyping()`；`C30`（沒有標記仍驗證）：標記為空字串時 `'' in key` 恆真，所有項目都被丟棄，結果同樣是空元組。
  - **8.1 的教訓**：我先前靠推理判斷的「等價」有一個判錯（`R33`，實際是測試被 `onUpdate` 後緊接 `onDone` 蓋住），所以改以差分模糊測試核對每一個；之後判斷等價一律先模糊再下結論。
- **9.2 設計文件**：更新 `docs/前端結構-frontend.md`（新增 3e-2「劇情卡、行動建議與歷史」含 Mermaid、載入順序、寫入呼叫由四個改為六個）、`docs/系統架構-architecture.md`（新增「RPG 行動建議與直接推進劇情」含時序圖與元件表、控制器、純運算模組、事件數二十四個、註冊函式、規格對照列）、`docs/模型呼叫層-llm-layer.md`（新增 6d `rpg_suggest`、用途列表與簡轉繁範圍）、`docs/營運與部署-operations.md`（兩個建議事件、保存期限列、假模型的行動建議員分支）、`docs/帳號與安全-auth-security.md`（建議端點的存取規則）、`docs/路線圖-roadmap.md`（狀態改為實作完成、等驗收）、根目錄 `README.md`（行動建議一節補前端與驗證）；`docs/資料模型-data-model.md` 與 `docs/project資料結構文件.md` 在批 2 已同步 0023，核對無需再改。驗證：`pytest tests/test_design_docs.py tests/test_readme.py tests/test_project_db_schema_doc.py` **107 通過**。
- **8.2 全套測試**（9.2 之後，輸出存檔於 scratchpad）：pytest **15022 通過、5 略過、0 失敗**（25 分鐘 0 秒，含 SQL Server `project_test` 兩軌；5 個略過皆既有）、Node **1789 通過、0 失敗**（原 1766，加上本批新增 23）。唯一警告是 `tests/test_assets_manifest.py` 的子行程輸出在 cp950 下解碼失敗（執行緒例外，測試本身通過），不在本 change 的改動範圍，未追查。
- **8.3**：`openspec validate rpg-story-view --strict` 通過；`alembic check` 在拋棄式 SQLite 從頭 `upgrade head` 到 0023 後「No new upgrade operations detected」。**沒有對開發 SQL Server `project` 跑 `alembic check`**（它還沒升到 0023，升級要先備份，須使用者同意）；SQL Server 軌的模型與遷移差異由全套裡的 `test_migrated_schema_has_no_difference_from_the_models` 對 `project_test` 驗證（通過）。
- **9.1 簡報衝突（只通知、沒改簡報）**：見 HANDOFF 與回覆；沒有改 `docs/2.0簡報.md`、PDF、`2.1簡報.pptx`、`4.0簡報.pptx`、六張圖解與產生器。
- **未做**：封存（須使用者驗收）、對展示環境的任何新部署（本批只改測試與文件；展示環境仍是 `787ee62`、RDS 0023）。

## 9. 文件與簡報通知

- [x] 9.1 依 `deck-conflict-watch` 檢查是否與 `docs/2.0簡報.md`、六張圖解、`docs/4.0簡報.pptx` 衝突，只通知使用者、不自行修改；驗證：把衝突位置與選項列給使用者
- [x] 9.2 更新 `docs/` 對應的設計文件（對照表見 `docs/README.md`：`docs/RPG.md`、前端結構、系統架構、資料模型、端點與模型呼叫流程、遷移），並讓 `tests/test_design_docs.py` 通過；驗證：`pytest tests/test_design_docs.py tests/test_readme.py tests/test_project_db_schema_doc.py` 通過

## Workflow follow-up

- 使用者驗收（含實機的翻頁手感、每段字數、載入起始位置、建議品質）通過後才封存。
- 封存後更新 `docs/路線圖-roadmap.md` 的狀態與日期，並複查各設計文件標頭的「對應 change」。
- 遊戲畫面相關的四支示範影片（開始冒險與主角建立、劇情對話與串流打字機、重新生成與編輯最後一輪、記事簿）於改版完成後重錄；展示環境的部署另案進行。
