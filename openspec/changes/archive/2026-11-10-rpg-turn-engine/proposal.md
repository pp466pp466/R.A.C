# Proposal

## Why

RPG 目前有劇本、主角與存檔（RC-1、RC-7、RC-9），也有遊戲幣的預扣、結算與退回（W1），但**沒有任何一段程式能把玩家的行動變成劇情**：`SessionService.begin_turn` 只建立一筆回合列，沒有呼叫模型、沒有串流、沒有世界狀態的更新、沒有輸出審查，預扣幣的 Service 也還沒有人呼叫。RC-2a 把這條管線做完整、做成可獨立驗收的後端：玩家送出一個行動，伺服器預扣幣、組裝上下文、串流生成劇情、逐句審查、再以一次結構化呼叫更新世界狀態，最後結算幣並完成回合。第 0 幕（開場）、NPC 角色卡與弧線階段、里程碑與結局都在這條管線上，拆開無法獨立驗收（`docs/RPG.md` 7.4、7.5、7.15）。

探索結論已寫在 `docs/RPG.md` 7.3 至 7.6 與 7.15；AGENTS.md 的對應修訂（RA-21：年齡可更正、只擋確定的未成年性內容）已於 `61b8a47` 套用。2026-11-05 對真實 RPG 模型的探測（7.2）顯示：它是推理型模型，預設下首個正文片段要 28 秒，**必須能關閉推理**才能串流；轉接器也必須把「結束原因為長度且正文為空」視為失敗。這些是這個 change 的前提。

## What Changes

- **回合管線與串流端點**（Web API，SSE）：`POST /api/game/sessions/<id>/turns/stream`。流程：驗證與冪等 → 預扣幣 → 提交並釋放資料庫交易 → 組裝上下文 → 串流呼叫 → 規則層逐句審查（保留一句緩衝，通過才釋放）→ 非串流的結構化狀態呼叫 → Service 驗證並寫入世界狀態 → 結算幣 → `done`。事件為 `meta`、`token`、`ping`、`transition`、`state`、`done`、`error`；每 15 秒心跳；後端偵測斷線時停止上游並退回預扣。另有唯讀的 `GET /api/game/sessions/<id>` 與 `GET …/turns?since_seq=`（驗收與 RC-3 同步用）。
- **模型轉接器串流**：`llm-openai-compatible` 新增串流呼叫（`stream_options.include_usage`、以短逾時輪詢的閒置 tick，使外層能送心跳與檢查逾時）、環境變數 `OPENROUTER_REASONING`（預設關閉，關閉時每個請求帶 `reasoning: {enabled: false}`）、「結束原因為長度且正文為空視為失敗」；新的 `llm-streaming` 能力定義串流呼叫的期限（首字 20 秒、單次生成 60 秒）、備援切換時機（只在第一個正文片段之前）與帳本寫入（含首字時間）。
- **上下文組裝**（純函式）：`system_instruction` 只放固定規則（分級、階段定義、來歷規則、標記字串、語言）；記事簿、世界狀態（含在場 NPC 切片與主角來歷）、劇本文字、最近 10 輪與第 0 幕全部放在使用者內容區的資料區塊；帶「語言」參數（預設 `zh-Hant-TW`）；最壞上下文有測試以全部上限驗證。
- **世界狀態與 NPC 角色卡**：每回合一次結構化狀態呼叫（`npc_updates`、`current_location`、`elapsed_months` 增量、`status_summary`、目標增減、`ending_proposed`），失敗重試 1 次，仍失敗則文字保留、世界狀態不變、回合仍算完成並收費。Service 驗證一切：階段只前進一階（`cracking` 可退回 `surface`，`revealed` 為終點）、新 NPC 一律從 `surface` 起、特質與切片有數量上限、存檔最多 30 位 NPC；**年齡由模型提議、Service 驗證後可往任何方向更正，每次更正在 `state` 事件帶通知並附更正前年齡**（RA-21）；里程碑由 Service 依年齡或年份判定，模型只在觸發後延伸目標；每回合 `elapsed_months` 增量有上限（資料庫設定表）。
- **輸出審查與被擋回合**：新的規則層輸出入口（跨句、依在場角色的年齡紀錄），對應表沿用 7.4：REJECT、`tier_mismatch`、`minor_suspected`、`real_person_suspected` 擋下，`other_concern` 放行並記錄類別；只在「年齡紀錄小於 18 且有性內容」或「文字明確寫出未成年的年齡或身分且與性內容同段或相鄰句」時加擋（年齡未知不單獨擋）。命中時停止上游、送 `transition`（伺服器端固定幾句繁體中文輪替）、自動重生一次並沿用同一筆預扣；仍被擋則 `done` 標記 `blocked`、退回預扣、解鎖輸入。被擋的文字留在 `memory_logs.llm_raw_response`（14 天）。
- **第 0 幕**：`POST /api/game/sessions/<id>/opening/stream`；狀態 `pending`／`generating`／`ready`／`fallback` 存在存檔上，不放進 `memory_logs`（不是一輪）；失敗或被擋退回劇本的初始對話；不扣幣、不計輪、token 計入全站每日用量；第一個玩家回合須等它 `ready` 或 `fallback`。
- **玩家主角的「來歷」**（本地人、穿越者）：角色卡新增列舉欄位，既有存檔視為本地人；AI 生成的預覽不決定來歷；穿越者由固定的 `system_instruction` 加入平台規則。主角年齡改為「開存檔時寫入，之後依 NPC 的相同規則更正」。
- **結局**：狀態呼叫可提議結局；Service 確認該劇本版本為自訂結局後才生效；壽終年齡由 Service 判定。該回合文字照常送完，之後存檔轉為 `ended`。
- **預扣冪等鍵加 `attempt`**：`memory_logs.attempt`，鍵為 `turn:{存檔}:{client_msg_id}:{attempt}`；卡在生成中逾時的回合可被接手（視同中斷、attempt 加 1）；同一存檔同時只處理一個進行中的回合。
- **資料與設定**：遷移 `0016_rpg_turn_engine`（只新增，expand）：`memory_logs.attempt`、`sessions.opening_text`／`opening_state`／`opening_started_at`、`llm_call_logs.first_chunk_ms`，並以 ORM 補一個設定表的鍵 `rpg.max_months_per_turn`。帳本新增 `kind`：`rpg_stream`、`rpg_state`、`rpg_opening`（`rpg_notebook` 屬 RC-2b）。新增 gunicorn 設定 `gunicorn.rpg.conf.py`（RPG 池的長連線設定；實際分流留給 RC-4）。
- **順手修正**：`flask llm-probe` 在 cp950 主控台遇到無法編碼的字元不再中斷。
- **不做**：同時串流上限與 503 文案（RC-4）；玩家重新生成與編輯輸入、回合前快照與 `revision`（RC-2c）；記事簿的抓取、玩家編輯與面板（RC-2b；上下文只讀取現有的記事簿文字）；任何畫面（RC-3）；`persona_patch` 與全站性格原型庫（RC-5）；好感度與狀態效果；RPG 的「一日被擋 3 次暫停對話」計數（規則層擋下玩家輸入不呼叫模型，沒有成本，RPG 沒有可暫停的對象；列為待確認）；法務覆核與選定正式模型後的提示詞驗收。

## Capabilities

### New Capabilities

- `rpg-turn-engine`: 回合的完整生命週期：開始（帳號閘門、輸入規則層、全站預算、第 0 幕未完成時等待、冪等與 attempt、單一進行中回合）、遊戲幣預扣／結算／退回的時機、完成（指派輪數、寫入世界狀態、狀態呼叫失敗的處理）、中斷與逾時接手、結局。
- `rpg-turn-stream`: 串流端點與 SSE 協定：路徑、事件、標頭、心跳、斷線處理、已完成回合的重送、唯讀端點、錯誤與狀態碼、快取與 CSRF。
- `rpg-turn-context`: 送給模型的上下文：固定的 `system_instruction`、資料區塊、視窗（最近 10 輪與第 0 幕）、在場 NPC 切片、來歷規則、標記字串、語言參數、最壞情況的大小驗證。
- `rpg-world-state`: 狀態呼叫的結構與 Service 驗證：NPC 角色卡與弧線階段、年齡驗證與更正通知、地點、經過月數增量上限、目標與里程碑、結局提議、世界狀態版本號。
- `rpg-output-review`: 劇情輸出的逐句審查與被擋回合：判定對應表、年齡與未成年用語的規則、保留一句緩衝、轉場文字、自動重生一次、被擋文字的保存。
- `rpg-opening`: 第 0 幕：狀態機、搶佔與接手、生成規則、失敗與被擋的退回、不計輪不扣幣、用量。
- `llm-streaming`: 串流模型呼叫的期限、備援切換時機、閒置 tick、用量估算與帳本欄位（首字時間）。

### Modified Capabilities

- `llm-openai-compatible`: 串流請求、`OPENROUTER_REASONING`、空正文的長度截斷視為失敗、探測指令的輸出編碼。
- `llm-structured-calls`: 帳本的用途新增 `rpg_stream`、`rpg_state`、`rpg_opening`，並記錄首字時間。
- `rpg-sessions`: 回合的 `attempt`、同一存檔單一進行中回合、逾時接手、存檔的初始狀態含第 0 幕與世界狀態結構。
- `rpg-protagonist`: 新增「來歷」；主角年齡改為開存檔時寫入、之後可依規則更正。
- `protagonist-suggestion`: 生成預覽不含來歷（由玩家選擇）。
- `runtime-limits`: 新增 RPG 組與 `rpg.max_months_per_turn`，兩個維運指令。
- `service-observability`: RPG 回合與第 0 幕事件只含白名單欄位。
- `personal-data-export`: 匯出納入第 0 幕。
- `container-runtime`: RPG 串流使用獨立的 gunicorn 設定。

## Impact

- **資料庫**：遷移 `0016_rpg_turn_engine`，全部可為空或有預設值（expand）；SQLite 與 SQL Server 雙軌都要通過。**同步 `docs/project資料結構文件.md`（字典、第 12 節遷移歷程）並在第 14 節追加異動紀錄**（使用者升級本機 `project` 之後）。
- **程式**：`app/llm/`（`types.py`、`transport.py`、`openai_compatible.py`）；新 `app/services/rpg_*`（上下文、世界狀態、輸出審查、串流執行、`game_service.py`）；`session_repository.py`、`models/rpg_session.py`、`models/llm_call_log.py`、`services/protagonist.py`、`protagonist_prompt.py`、`runtime_limits.py`、`config.py`、`cli.py`；新 `app/controllers/game_api_controller.py` 與 `app/observability/rpg_events.py`；`registry.py`、`cache_headers.py`；`gunicorn.rpg.conf.py`；`.env.example`。
- **測試**：轉接器串流（以假傳輸）、串流執行期限與備援時機、上下文最壞情況、世界狀態驗證（階段、年齡、地點、里程碑、結局）、輸出審查（跨句、誤擋與漏擋樣本）、回合管線（冪等、attempt、預扣／結算／退回、並行、逾時接手）、第 0 幕狀態機、SSE 端點、日誌掃描、遷移雙軌、架構測試。另寫 HTTP 驗收腳本（事件序列、扣幣、被擋回合、結局、中斷退幣），並以真實 OpenRouter 模型各跑一次第 0 幕與三個回合（只作驗證，不算測試）。
- **文件**：`docs/RPG.md`（7.15 標示已立案）、`docs/資料模型-data-model.md`、`docs/project資料結構文件.md`、`docs/系統架構-architecture.md`、`docs/模型呼叫層-llm-layer.md`、`docs/營運與部署-operations.md`、`docs/路線圖-roadmap.md`、README（端點表、環境變數、指令、驗收紀錄）。AGENTS.md 不再修改（RA-21 已套用）。
- **不影響**：客服、登入、劇本創作與審查、錢包 API、既有存檔的讀取與匯出。
