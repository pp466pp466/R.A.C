# Tasks

> 分批與測試節奏：apply 之前先依 `openspec-batch-workflow` 提分批方案並由使用者確認；每批只跑「相關測試加全域守門子集」（`tests/architecture`、`test_service_errors`、`test_design_docs`、`test_readme`、`test_restricted_session`、`test_legal_pages`、`test_agents_skill_sync`）。動到後端（`app/`）、端點、遷移、抹除或權限路徑的批次才加跑相關的後端測試；全套 pytest 只在第 8 組。

## 1. 規範修訂（須使用者確認文字後才寫）

- [ ] 1.1 依 `design.md` D9 的草案修訂 `AGENTS.md` 第 5、6、7、10、11 節（最小改動），先把每一節的新舊對照給使用者確認；驗證：使用者確認，且 `git diff AGENTS.md` 只有草案列出的段落
- [ ] 1.2 同步 `.claude/skills/python-flask-skill` 的對應 reference，並在 `tests/test_agents_skill_sync.py` 的 `SYNCED_RULES` 補守門句；驗證：`pytest tests/test_agents_skill_sync.py` 通過，且新增的守門句在 AGENTS.md 與 reference 都找得到

## 2. 後端：直接推進劇情的平台規則

- [ ] 2.1 在 `rpg_prompt.py` 新增 `ADVANCE_ACTION = "推進劇情"` 與回合敘事專用的推進規則（只進回合敘事的系統指令，不進第 0 幕、狀態、記事簿與之後的建議）；驗證：新增的 `tests/test_rpg_advance_prompt.py` 斷言規則只出現在回合敘事、安全模式界線仍在、玩家文字不在系統指令
- [ ] 2.2 確認「推進劇情」走一般回合的預扣、規則層、重新生成與編輯，不需特例；驗證：以假模型跑一輪 `action="推進劇情"`，斷言預扣 30 幣、`insufficient_coins` 路徑、重新生成可用、同一 `client_msg_id` 重送不重複扣幣（沿用既有回合測試的夾具）
- [ ] 2.3 跑相關測試與全域守門子集；驗證：通過

## 3. 後端：資料結構、快照與匯出

- [ ] 3.1 在 `RpgSession` 新增 `suggestions_json`（JSON、可為空），撰寫遷移 `0023`（expand，`recreate="never"` 的 downgrade），驗證：升降級測試與 `alembic check`（SQLite）通過；舊資料列讀出為空值
- [ ] 3.2 在 `session_repository.py` 新增讀取目前錨點與保存建議的方法（所有方法帶 `user_id`；寫入不改 `version`；寫回前比對錨點的條件式更新）；驗證：repository 測試覆蓋錨點相等才可讀、錨點改變後不可讀、並行寫入只保留一份、他人存檔回無資料
- [ ] 3.3 `session_snapshot` 帶出 `suggestions`（有效才有；不含錨點細節）；驗證：快照測試覆蓋新存檔、有效、完成新回合後過期、修訂成功後過期
- [ ] 3.4 `personal_export_service` 的 `SESSION_COLUMNS` 與 JSON 匯出加入建議；驗證：匯出測試覆蓋有建議與沒有建議（JSON 字串陣列、CSV 換行連接、公式前綴規則照舊）
- [ ] 3.5 確認存檔刪除與帳號抹除涵蓋新欄位（同一資料列，預期不需改程式）；驗證：既有的抹除測試加一項斷言「建議隨存檔消失」
- [ ] 3.6 同步 `docs/project資料結構文件.md`（欄位表、第 14 節異動紀錄）並用既有的列骨架工具核對；驗證：`pytest tests/test_project_db_schema_doc.py` 通過
- [ ] 3.7 跑相關測試與全域守門子集；驗證：通過

## 4. 後端：建議的提示詞、服務與端點

- [ ] 4.1 新增純函式模組 `rpg_suggest_prompt.py`（固定系統指令樣板、`SUGGEST_SCHEMA`、`clean_items`、洩漏標記語意規則）並列入架構測試的純模組清單；驗證：單元測試覆蓋 NFC 與控制字元、長度 2 至 30、網址與電話與 email 樣式、去重、標記、至多 3 項、全部無效時回空
- [ ] 4.2 在 `rpg_context.py` 新增 `build_suggest_context`（玩家可見世界、角色卡與來歷、最近 3 輪或第 0 幕；不含記事簿、NPC 切片、隱藏欄位）；驗證：測試以含隱藏特質與弧線階段的存檔組裝，斷言不出現；玩家文字只在使用者內容區；生效模式現算
- [ ] 4.3 `llm_call_log.py` 新增 `KIND_RPG_SUGGEST`、加入 `CALL_KINDS` 與 `TRADITIONAL_KINDS`；驗證：既有的帳本與用途列舉測試更新後通過，且帳本不含文字
- [ ] 4.4 新增 `SuggestionService`（前提檢查順序、重用、預算檢查、`run_structured`、規則層與安全模式過濾、寫回前再比對錨點、`suggestions_stale`）；驗證：服務測試覆蓋 `design.md` D5 的十個步驟，包含失敗不保存、重用不呼叫模型、不扣幣、備援模型、管理員相同行為
- [ ] 4.5 在 `game_api_controller.py` 新增 `POST /api/game/sessions/<id>/suggestions`（JSON、CSRF、閘門）；驗證：端點測試覆蓋 404、`session_ended`、`opening_pending`、`turn_in_progress`、`system_busy`、沒有供應商、`suggestions_unavailable`、`suggestions_stale` 與成功回應的欄位；`/api/*` 回應不洩漏內部細節
- [ ] 4.6 在 `rpg_events.py` 新增建議事件（白名單：內部編號、項數、是否重用、結果列舉）；驗證：日誌測試收集一輪建議流程，找不到任何建議、劇情或行動的文字
- [ ] 4.7 確認呼叫的狀態：一個進行中（`streaming`）的回合而尚無完成回合時，錨點與 `turn_in_progress` 判斷正確；驗證：新增對應測試
- [ ] 4.8 跑後端相關測試（`tests/test_rpg_*`、`tests/test_game_*`、帳本與遷移）與全域守門子集；驗證：通過

## 5. 前端純函式：分頁與閱讀器

- [ ] 5.1 新增 `story-pages.js`（UMD）：句界、引號深度、換行硬分界、3 句與 150 字上限、單句超長的切法、字素安全、`done` 與湊滿才釋出；驗證：`tests/js/story-pages.test.js` 涵蓋 `rpg-story-pages` 規格的分段與串流釋出情境，並有「串接還原原文」的性質測試（多組含標點、無標點、對話、表情符號的輸入）
- [ ] 5.2 新增 `story-reader.js`（UMD，注入時鐘與打字機工廠）：`feed`、`next`、`prev`、`jumpToEnd`、`reset`、`restore`、等待狀態、`prefers-reduced-motion`；驗證：`tests/js/story-reader.test.js` 以假時鐘涵蓋打字中補完、翻頁、第一段停用 `‹`、回看直接完整顯示、串流等待、`done` 補完、被擋與中斷、修訂還原
- [ ] 5.3 跑 Node 測試與全域守門子集（含前端架構守門）；驗證：通過，新檔沒有 CDN、`v-html`、Bootstrap JavaScript

## 6. 前端：狀態、API 與接線

- [ ] 6.1 `game-state.js` 新增 `ADVANCE_ACTION`、劇情卡視圖、建議與推進的啟用條件與錯誤對照（含 `suggestions_unavailable`、`suggestions_stale`、`opening_pending`）；驗證：`tests/js/game-state*.test.js` 擴充後通過；新增斷言前後端的 `ADVANCE_ACTION` 相等
- [ ] 6.2 `game-api.js` 新增建議端點呼叫（axios、逾時 35 秒、AbortController）；`game-store.js` 讀取快照的 `suggestions`，且確認建議不寫入 IndexedDB；驗證：Node 測試涵蓋請求、取消、重用，並斷言儲存層沒有建議
- [ ] 6.3 `game-session.js` 把回合與第 0 幕的串流事件餵給 reader，載入既有存檔時 `jumpToEnd`，處理鍵盤翻頁（焦點與 Modal 判斷）、推進動作、建議動作（貼進輸入框並收回）與離開頁面中止；驗證：沙盒跑完整載入與一輪流程，攔截 `console` 後沒有玩家文字；涵蓋被擋轉場、中斷、重送、離線與已完結
- [ ] 6.4 `game-revision.js` 改掛劇情卡：按鈕只在當前顯示最後一輪時出現；成功後從第一段重新閱讀、失敗還原舊內容與原本的段；驗證：`tests/js/game-revision*.test.js` 更新後通過，涵蓋 `rpg-revision-ui` 規格的情境
- [ ] 6.5 跑 Node 測試與全域守門子集；驗證：通過

## 7. 前端：版面與樣式

- [ ] 7.1 改寫 `session.html`：劇情卡、說話者標籤、`‹`、等待狀態、行動區（建議圖示與 chips、直接推進劇情、輸入框）、歷史 Modal（`v-if`、純文字、載入更早內容）、桌機左側捷徑欄；全部以 `{{ }}` 渲染，沒有 `v-html` 與 `data-bs-*`；驗證：頁面測試（`tests/test_game_page*.py`）更新後通過，架構守門通過
- [ ] 7.2 `game.css`：純 CSS 氛圍背景層（專案色彩、無圖片、無純白）、劇情卡大字、桌機左欄與手機的按鈕位置、icon 與 chips 樣式（沿用既有 chips 規格）、`prefers-reduced-motion`；驗證：用無頭 Chrome 在 1280 與 360 寬度截圖逐張目視，360px 沒有橫向捲軸、輸入區不遮住劇情卡
- [ ] 7.3 建議圖示使用內嵌 SVG 並含可及性標籤；劇情卡的完整文字另提供給輔助科技（打字中不重複朗讀）；驗證：頁面測試斷言標籤與屬性存在
- [ ] 7.4 跑 Node、頁面測試與全域守門子集；驗證：通過；並以假模型的驗收環境實際操作一輪（翻頁、回看、歷史、推進、建議、重新生成、編輯），記錄結果給使用者

## 8. 整體驗證

- [ ] 8.1 變異驗證：對 `story-pages`、`story-reader`、`rpg_suggest_prompt.clean_items` 與 `SuggestionService` 的關鍵判斷（句界、湊滿釋出、錨點比對、規則層過濾、不扣幣）做變異，設定測試逾時與行程逾時；驗證：每個變異都被測試殺死，工作樹還原乾淨
- [ ] 8.2 全套 pytest 與全套 Node 測試（背景執行、輸出存檔）；驗證：全部通過，記錄通過與略過的數量
- [ ] 8.3 `openspec validate rpg-story-view --strict` 與 `alembic check`；驗證：兩者通過

## 9. 文件與簡報通知

- [ ] 9.1 依 `deck-conflict-watch` 檢查是否與 `docs/2.0簡報.md`、六張圖解、`docs/4.0簡報.pptx` 衝突，只通知使用者、不自行修改；驗證：把衝突位置與選項列給使用者
- [ ] 9.2 更新 `docs/` 對應的設計文件（對照表見 `docs/README.md`：`docs/RPG.md`、前端結構、系統架構、資料模型、端點與模型呼叫流程、遷移），並讓 `tests/test_design_docs.py` 通過；驗證：`pytest tests/test_design_docs.py tests/test_readme.py tests/test_project_db_schema_doc.py` 通過

## Workflow follow-up

- 使用者驗收（含實機的翻頁手感、每段字數、載入起始位置、建議品質）通過後才封存。
- 封存後更新 `docs/路線圖-roadmap.md` 的狀態與日期，並複查各設計文件標頭的「對應 change」。
- 遊戲畫面相關的四支示範影片（開始冒險與主角建立、劇情對話與串流打字機、重新生成與編輯最後一輪、記事簿）於改版完成後重錄；展示環境的部署另案進行。
