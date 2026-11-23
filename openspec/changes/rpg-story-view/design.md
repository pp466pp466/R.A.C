# Design

## Context

動機與範圍見 `proposal.md`；行為契約見各 delta 規格（`rpg-story-pages`、`rpg-suggestions`、`rpg-advance-action`，以及修改的 `rpg-game-screen`、`rpg-stream-client`、`rpg-turn-context`、`rpg-revision-ui`、`rpg-sessions`、`personal-data-export`）。

現況（已讀程式確認）：

- 遊戲頁 `session.html` 以 `items`（`game-state.js` 的 `timelineFrom`）渲染聊天式時間軸；每輪一個 `gm-story` 氣泡，打字機在串流層（`UI.TypewriterBuffer`，片段推入、`done` 後補完）。重新生成與編輯的按鈕掛在最後一輪氣泡下方（`game-revision.js`）。
- 每輪有兩個模型呼叫：劇情串流（`rpg_stream`）與狀態呼叫（`rpg_state`，結構化輸出，`STATE_SCHEMA`），第 0 幕為 `rpg_opening`，每 10 輪另有 `rpg_notebook`。結構化呼叫統一走 `LlmService.run_structured`（驗證、重試、切備援、寫帳本）。
- `memory_logs` 每回合有 `seq`、`revision`；`session_snapshot` 已帶 `last_turn`（含 `revisable` 與不可修訂原因 `in_progress`）。第 0 幕在 `sessions.opening_*`。
- 玩家行動的規則層是 `script_rules.check_text_rules`（被擋回 422 `input_blocked`），安全模式的性內容判斷是 `mentions_sexual`。全站 token 上限以 `_budget_exhausted()` 檢查。
- 前端規範：Vue 3 全域變數模式、狀態完全由 Vue 接管、不用 Bootstrap JS、不用 `v-html`、離線資源；純函式模組採 UMD（如 `typewriter.js`）以便 Node 測試。
- AGENTS.md 與 `python-flask-skill` 的 reference 逐字一致，由 `tests/test_agents_skill_sync.py` 守門。

## Goals / Non-Goals

**Goals:**
- 分頁與閱讀狀態都是前端純函式，可用 Node 以假時鐘完整測試；不改後端的串流事件協定、回合內容、扣幣與審查。
- 建議是**使用者主動才花的 token**：預設零呼叫、每畫面最多一次、結果存檔重用。
- 失敗路徑不弄壞既有行為：建議失敗不影響回合、扣幣或畫面其他部分。

**Non-Goals:**
- 不做 `自動`、`x2`、空白送出、跳到某一輪的歷史導覽、背景圖片、「換一組」、建議存 localStorage。
- 不改狀態呼叫與記事簿；不讓建議進入上下文。
- 不處理劇情文字的品質調整（例如要求模型輸出短句）；分頁只依現有文字切。

## Decisions

### D1. 分頁是純函式，段在「湊滿」時才釋出
`app/static/js/game/story-pages.js`（UMD）：`paginate(text, { done })` 回傳 `{ pages, pending }`。步驟：①以換行切成區塊（硬分界，並重設引號深度）；②區塊內掃描句界：句末標點 `。！？…`（連續者視為一組）及緊接的收尾 `」』）)` 為句界，**引號深度大於 0 時不算句界**（避免切開對話）；③貪婪成段：至多 3 句、累計超過 150 字時在句界之前切；單句超過 150 字先於 `，；、` 切，仍無處才在 150 字處以 `Intl.Segmenter` 字素邊界切；④`done` 為假時，只輸出「已湊滿」的段（3 句、後面已有換行、或下一句會超過上限），其餘成為 `pending` 尾端不釋出。

選擇「湊滿才釋出」而不是「有兩句就先秀」：貪婪分組在後續句子到達時會改變已秀出的段邊界，閱讀中的文字會跳動；湊滿才釋出保證已釋出的段永不改變，代價是第一段要等到約三句（數秒）才出現，期間顯示「你」的行動頁與載入狀態。
替代案：後端分段並以事件送出——否決：要改串流協定與審查緩衝，且重送與歷史都要重做；前端純函式零後端風險。

### D2. 閱讀器是純狀態機，打字機只負責「當前段」
`app/static/js/game/story-reader.js`（UMD，注入時鐘與打字機工廠）：狀態＝當前輪的鍵、段陣列（含第 0 段的「你」行動頁）、`index`、`typing`、`waiting`、`finished`。動作：`feed(text, done)`、`next()`（打字中先補完、再翻頁）、`prev()`（`index` 為第一段時無作用）、`jumpToEnd()`（載入既有存檔）、`reset(turnKey)`、`restore(snapshot)`（修訂失敗還原）。`UI.TypewriterBuffer` 不改介面，改成「每翻到新段 `play(段文字)`」；回看已讀段直接顯示完整文字。`prefers-reduced-motion` 時一律直接完整顯示。
`game-session.js` 只負責把串流事件餵給 reader，並由 Vue 響應式變數暴露 `card`（說話者、文字、索引、總段數、可前進／可回上一段、等待中）。

### D3. 主畫面只有當前輪；歷史是同一份資料的另一個視圖
`timelineFrom` 的 `items` 保留（歷史與同步都用它），主畫面改取「最後一個項目」作為當前輪。歷史是 `v-if` 的自製 Modal，內容沿用 `items` 與既有的「顯示更早的內容」；沒有導覽。載入既有存檔時 reader 以 `jumpToEnd()` 啟動（最後一段、不打字），理由：玩家重新整理多半是要接續行動；`‹` 仍可回看。這是**未經使用者逐項確認的預設**，列在 Open Questions。

### D4. 「直接推進劇情」是前端常數加固定的平台規則
前端常數 `ADVANCE_ACTION = '推進劇情'`（`game-state.js`）；後端常數放 `rpg_prompt.py`，在**回合敘事專用**的樣板（與 `_ACTION_RULE` 同層，不進第 0 幕、狀態、記事簿與建議）加入規則：「本次行動恰為『推進劇情』時，玩家沒有特別指示：依最近 10 輪、記事簿與當前世界狀態自然推進，可讓世界或其他人物主動行動；不得替主角做違背其角色卡的重大決定；內容界線不因此放寬」。測試以 Node 讀出前端常數、Python 讀後端常數，確認兩者相等。行動走既有的 `begin_turn`，所以預扣、規則層、重新生成與編輯自然沿用；不做任何特例。

### D5. 建議：獨立端點、獨立服務、純函式提示詞
- `app/services/rpg_suggest_prompt.py`（純函式，列入 `tests/architecture` 的純模組清單）：固定 `system_instruction` 樣板（語言、生效模式界線、「只提議主角可做的行動、每個 2 至 30 字、第一人稱或祈使短句」、資料區塊聲明、洩漏標記）、`SUGGEST_SCHEMA`（`suggestions`：字串陣列，最多 5 項，模型多給也沒關係）、`clean_items(raw, ...)`（逐項驗證，回傳至多 3 項）、語意規則（洩漏標記）。
- 上下文（`rpg_context` 新增 `build_suggest_context`，純函式）：玩家可見世界（地點、進行中的目標、狀態摘要）、主角角色卡（含來歷）、最近 3 輪的行動與劇情（尚無完成回合時為第 0 幕文字）。**不帶**記事簿、NPC 切片、弧線階段、隱藏歸屬與劇本隱藏設定；因此「不洩漏隱藏資訊」由資料來源保證，不靠模型自律。
- `app/services/rpg_suggest_service.py`（`SuggestionService`）：①`require_chat_access`；②`get_owned`（沒有回 404）；③以 `revision_state` 取得錨點與「進行中／已完結」判斷（與修訂共用同一判斷，畫面與端點不各說各話；只有一個 `streaming` 回合而沒有完成回合的情形要實測）；④第 0 幕未完成回 `opening_pending`；⑤保存的錨點等於目前錨點 → 直接回傳（`reused`）；⑥`_budget_exhausted()` → `SystemBusy`；⑦`run_structured(kind=KIND_RPG_SUGGEST, model=RPG 模型)` 的短預算（沿用單次請求 30 秒總預算）；⑧`clean_items` 後以規則層逐項過濾；⑨寫回前再讀一次錨點，若已改變（呼叫期間有新回合）回 409 `suggestions_stale` 且不保存；⑩條件式寫入 `sessions.suggestions_json`（不碰 `version`）。
- 控制器：`POST /api/game/sessions/<id>/suggestions`（JSON 端點，不是 SSE），控制器不寫權限判斷。前端以 axios（逾時 35 秒、AbortController）。
- `llm_call_logs` 新增用途 `rpg_suggest`（欄位寬度 20 已足夠）；加入 `TRADITIONAL_KINDS`（輸出須為繁體中文的檢查）；事件日誌只記內部編號、項數與是否重用。

替代案：①併入每輪狀態呼叫（HANDOFF 的原預設）——否決：使用者要求「不點就不產生」，併入會每輪都花 token 且「狀態失敗＝整個失敗」的耦合需要改規格；②每次點 icon 都重新產生——否決：重開 icon 會重複花費，且要另外計次，反而需要保存計數；以錨點比對天然達成「每畫面一次」。

### D6. 建議的保存格式與陳舊判斷
`sessions.suggestions_json`（JSON、可為空）：`{"seq": <最後完成回合的 seq，第 0 幕為 0>, "revision": <該回合 revision，第 0 幕為 0>, "items": [...]}`。錨點相等才有效；新回合完成使 `seq` 改變、修訂成功使 `revision` 加 1，都自動使舊建議過期，不需要在回合引擎或修訂流程中清除（因此**不改動 `TurnRunner`、`RevisionRunner`**，降低回歸風險）。過期內容留到下次被覆寫或隨存檔刪除；個資定位與存檔相同。寫入不改 `version`，避免與世界狀態的樂觀鎖互相干擾。快照的 `suggestions` 欄位由 `session_snapshot` 現算，只有有效時才帶出。

### D7. 遷移 `0023` 與文件
單一 expand 步驟：`op.add_column('sessions', sa.Column('suggestions_json', sa.JSON(), nullable=True))`，無預設值；downgrade 以 `recreate="never"` 直接 DROP（沿用 0010 以後的做法，避免 SQLite 重建資料表時串聯刪除）。純 ASCII 規則只限 `alembic.ini` 與 `script.py.mako`，遷移腳本用 UTF-8 繁體中文註解。同步 `docs/project資料結構文件.md`（欄位表、ERD 若有、第 14 節異動紀錄；有測試守門與列骨架工具）。`personal_export_service` 的 `SESSION_COLUMNS` 加 `suggestions`。開發 SQL Server 的實際升級不在這個 change（部署步驟，須先備份）。

### D8. 前端結構與檔案分工
新增：`story-pages.js`、`story-reader.js`（純函式，Node 測試）。修改：`game-state.js`（`ADVANCE_ACTION`、`timelineFrom` 之外的 `cardView`、建議的前置條件 `canSuggest` 與錯誤對照）、`game-session.js`（餵 reader、推進與建議動作、鍵盤）、`game-store.js`（快照的 `suggestions`；**不**把建議寫入 IndexedDB）、`game-api.js`（建議端點）、`game-revision.js`（按鈕改掛劇情卡；還原與重新從第一段閱讀）、`turn-stream.js`（事件照舊，只改接收端）、`game.css`（氛圍層、劇情卡、左欄捷徑、icon 與 chips）、`session.html`。建議圖示用內嵌 SVG（不引入圖示字型）。鍵盤事件掛在頁面層，判斷焦點元素與是否有開啟的 Modal。

### D9. AGENTS.md 與 skill 的最小修訂（apply 第一批，須使用者確認後才寫）
草案（逐節，皆為增補或改寫一句，不動既有條文的其他部分）：
- 第 5 節「UI 平滑渲染」：RPG 的片段文字依句分段，以「段」為單位逐字渲染，點擊或按鍵翻段，打字中第一下補完；客服維持完整 reply_text 的打字機。
- 第 6 節：行動建議不進記事簿與上下文（RPG 的「當前世界狀態」之外不新增送給模型的內容）。
- 第 7 節：新增兩條——「遊戲畫面（RPG）」（視覺小說式分段、翻頁、當前輪回翻、歷史只讀全文、純 CSS 氛圍背景、圖片 v1 不實作）與「行動建議與直接推進劇情（RPG）」（點圖示才產生、每畫面最多一次、不扣幣但計入 token 上限、固定文字「推進劇情」為一般行動）；「遊戲幣」與「重新生成與編輯」各補一句（建議不另扣、推進等同一般行動）。
- 第 10 節：建議與歷史純文字渲染；建議須通過與玩家行動相同的規則層。
- 第 11 節：建議由玩家文字衍生，與存檔同期限、隨存檔與帳號刪除、納入匯出、日誌不記內容。
同步 `python-flask-skill` 的對應 reference，並在 `tests/test_agents_skill_sync.py` 補守門句。

### D10. 測試策略
- Node：`story-pages`（含引號、換行、超長句、字素、還原不丟字的性質測試）、`story-reader`（假時鐘：補完／翻頁／回看／串流釋出／修訂還原／減少動態）、`game-state`（卡片視圖、可用條件、錯誤對照）、`game-session`（沙盒跑完整一輪且攔截 `console`）。
- pytest：提示詞純函式（玩家文字不在系統指令、不含隱藏資訊、推進規則只在回合敘事）、`clean_items`、服務（前提、重用、陳舊、失敗不保存、不扣幣、帳本、備援）、端點（404、409、503）、遷移升降級、匯出、快照、架構守門（純模組、無 `v-html`、無 Bootstrap JS）、`test_design_docs`。
- 依「測試節奏約定」：中間批次只跑相關測試加全域守門子集；後端批（`app/`、端點、遷移）與最後一批才跑全套與變異驗證。

## Risks / Trade-offs

- [分頁品質：模型回覆沒有標點或全是對話] → 單句超長有逗號與 150 字的保底切法；引號內不切避免切開對話，但對話超長時靠 150 字保底；分頁純函式有性質測試（不丟字）。
- [第一段延遲：湊滿三句才釋出] → 串流本來就是首字快、整段數秒；期間顯示「你」的行動頁與載入狀態；若實測延遲太長，可改為「兩句即釋出」而不改架構（常數）。
- [建議呼叫失敗後可無限重試而重複花 token] → 失敗不保存、不佔次數是刻意的（玩家沒拿到東西）；入口層限流（Nginx `limit_req`）與全站每日 token 上限是第二道；若日後濫用，再加每存檔每小時的失敗次數上限（需要新的資料欄位，列為後續）。
- [並行雙擊或兩個裝置同時按圖示，會多呼叫一次] → 前端有忙碌旗標；後端寫回時以錨點與「目前無有效建議」的條件式更新，後到者讀到已存的結果並回傳（`reused`），只多花一次 token，不破壞「每畫面一份」。
- [建議內容被玩家視為劇情劇透或洩漏隱藏設定] → 上下文只含玩家可見資訊，從資料來源保證；測試以含隱藏特質的存檔驗證不出現。
- [建議含性內容或絕對禁止項] → 逐項過規則層與安全模式界線；模型提議的行動送出時仍會再過一次規則層（玩家可修改後送出），所以不是唯一防線。
- [重寫主畫面的回歸風險：重新生成、編輯、重送、被擋轉場、離線、已完結都依賴舊氣泡結構] → 時間軸資料層（`items`、同步、IndexedDB）不動，只換呈現；`game-revision.js` 的還原與覆寫邏輯以 reader 的 `restore` 銜接；Node 與頁面測試逐項覆蓋；改版完成前保留舊版畫面測試的對照。
- [AGENTS.md 與 skill 逐字同步遺漏] → `test_agents_skill_sync` 守門；在同一批修改兩邊。
- [遊戲畫面相關示範影片與簡報過時] → 不在這個 change 修；依 `deck-conflict-watch` 通知，影片於改版完成後重錄。

## Migration Plan

1. 備份後執行遷移 `0023`（expand：只新增可為空欄位）。
2. 部署新版程式。舊版程式在新結構上照常運作（不認識新欄位）；新版程式在舊結構上不要求運作，所以順序固定為先遷移再部署。
3. 回復：新版程式回退到舊版時，`suggestions_json` 內容被忽略；downgrade 只在沒有真實資料時使用（會遺失已保存的建議，不影響其他資料），有資料時以備份還原。
4. 展示環境的部署與重錄影片不在這個 change；部署用既有的 `aws_demo.py deploy`。

## Open Questions

- 載入既有存檔時主畫面停在「最後一段」還是「第一段」（D3）：先採最後一段；若使用者實際使用後覺得會漏看，改成第一段只是改 `jumpToEnd` 的目標，不影響規格結構（但要同步修改 `rpg-story-pages` 的對應需求）。
- 打字機速度與每段字數上限 150 的手感：先用現有的打字機參數與 150 字，實機調整只動常數。
- 桌機左欄捷徑的版面細節（欄寬、是否可收合）：依實作時的畫面決定，不影響行為規格。
