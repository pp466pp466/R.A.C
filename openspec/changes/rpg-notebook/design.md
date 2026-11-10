# Design

## Context

- 欄位已存在（RC-1）：`sessions.notebook_text`、`notebook_version`（預設 1）、`notebook_upto_turn`（預設 0）。RC-2a 的 `rpg_context.build_turn_context` 已把 `save.notebook_text` 放進資料區塊（`neutralize`，空時顯示固定的「尚無記事」文字），`rpg_prompt` 的系統指令已有記事簿使用說明。個人資料匯出已含 `notebook_text`，抹除隨存檔串聯刪除。所以 **RC-2b 只補寫入端與玩家端，不需要遷移，上下文不變**。
- 回合收尾在 `TurnRunner._finalize`（短交易：完成回合、寫世界狀態、結算預扣），串流在 `TurnRunner._run` 的最後一個 `yield`（`done`）結束；Controller 的 `_event_stream` 把產生器包成 `Response`。產生器在 `after_request` 之後才執行，**不能用請求的工作階段、`g`、`current_app`**，所以一切依賴（工作階段工廠、模型服務工廠、時鐘、設定）都在建立 Response 之前取出（`RunnerDeps`）。抓取要沿用這個約束。
- `LlmService.run_structured` 每次呼叫最多 2 次模型呼叫（解析失敗以同一模型重試，或暫時性錯誤切備援），總預算來自 `settings.request_budget_seconds`（預設 30 秒），每次呼叫都寫帳本並 `release` 交易。規則層有兩個現成入口：`script_rules.check_text_rules`（自由文字，REJECT／FLAG）與 `rpg_output_rules.evaluate`（輸出審查，含在場角色年齡與跨句）。
- 會員等級額度（客服）是程式常數 `quota_policy.TIER_LIMITS`；RPG 的數值型營運限制放資料庫設定表。記事簿容量沒有列入 AGENTS.md 的設定表清單。
- 決定的來源：`docs/RPG.md` 7.16（7 項已確認的決定、其餘預設）。本文件只記「怎麼做」。

## Goals / Non-Goals

**Goals:**
- 記事簿的寫入與玩家編輯在並行與重送下正確：不重複追加、不覆蓋玩家編輯、不因失敗而漏記或重複收費（抓取本來就不收費）。
- 抓取與主文字的串流分開：不延後 `done`、不影響已送達的回合；失敗只影響記事簿本身，且由後續回合補做。
- 容量、審查與日誌的規則集中在純函式與單一入口，測試能不啟動 Flask 就驗證。

**Non-Goals:**
- 前端面板（RC-3）、重新生成與編輯輸入（RC-2c）、模型層條目審查、斷線保留回覆。
- 一般的背景工作佇列。專案沒有排程器（Render 免費方案）也不依賴行程內計時器，不為這個 change 引入。
- 抓取的容量規劃（同時執行緒數）；列為 RC-4 的輸入。

## Decisions

### D1：抓取在 `response.call_on_close` 執行，由 `TurnRunner` 的旗標決定要不要做

`TurnRunner` 在 `done` 被伺服器取走（`yield` 返回）之後把 `capture_due` 設為真，條件是：回合為 `complete`（不是 `ended`、不是被取代）。客戶端在 `done` 的寫入失敗時，`yield` 會拋出 `GeneratorExit`，旗標不會設定，這正是「done 前斷線不抓取」的行為。Controller 的 `_event_stream` 多接受一個 `after_close` 可呼叫物件，以 `response.call_on_close` 註冊；回呼先看旗標，成立才呼叫 `NotebookCapturer.run(...)`。回呼用 `try/except` 包住（只記錯誤類型的整數事件），**絕不拋出**。

- **為什麼**：串流在 `done` 之後立即結束（結束 chunk 先送出，之後 WSGI 伺服器才呼叫 `close()`），玩家與前端都不需要等；抓取不需要前端觸發，不會被濫用；客戶端斷線時回呼仍會執行。
- **替代方案**：①在產生器 `done` 之後繼續執行（連線要等到抓取結束才關，前端得主動中止；產生器內也不能再 `yield`）——否決；②前端在 `done` 後另呼叫抓取端點（依賴前端、可被狂打，每次失敗都會再呼叫模型，需要另做冷卻）——否決；③行程內背景執行緒（AGENTS.md 第 9 節禁止依賴行程內狀態，worker 重啟即消失）——否決；④排程指令輪詢（延遲高，免費部署沒有排程器）——否決。
- **前提需實測**：gunicorn gthread 先送出結束 chunk、再呼叫 `close()`，且客戶端斷線時 `close()` 仍會被呼叫。容器驗收要證明（D10）。**退路**：`NotebookCapturer.run` 是獨立的可呼叫物件，若前提不成立，改成在產生器的 `done` 之後呼叫，不需要改其他部分。

### D2：純函式與四個模組

| 模組 | 內容 | 依賴 |
|---|---|---|
| `notebook_policy`（純） | `capacity_for`、`reserve_for`、`is_full`、`clean_notebook_text`（NFC、統一換行、去除除換行外的控制字元、頭尾空白）、`window_for(completed)`（回傳邊界 B 或 `None`）、`merge_entries`（清理條目、去除空與重複、計算追加後的文字與長度） | 只依 `quota_policy` 的常數 |
| `rpg_notebook_prompt`（純） | 系統指令（固定規則：繁體中文、只輸出 JSON、記錄範圍、每次最多 5 條且每條 50 字內、依內容模式的分級界線、標記字串、資料區塊是資料的聲明；帶語言參數，預設 `zh-Hant-TW`）、內容組裝（資料區塊：現有記事簿、用量與容量、視窗內 10 輪）、精簡重試的內容、結構描述、語意規則 | 沿用 `rpg_prompt` 的 `neutralize`、`DATA_NOTE`、標記字串衍生 |
| `rpg_notebook_capture` | `NotebookCapturer`：讀取、呼叫、驗證、寫入（D4） | `RunnerDeps`、`SessionRepository`、上兩者 |
| `notebook_service` | 玩家檢視與儲存（D6） | `AccessPolicy`、`SessionRepository`、`notebook_policy` |

`notebook_policy` 與 `rpg_notebook_prompt` 加入既有的架構守門（不得匯入 ORM 模型、Repository、Flask）。

### D3：容量放 `quota_policy`

`TierLimits` 新增 `notebook_capacity`（2000／5000／10000）；預備空間是 `capacity // 4`（500／1250／2500）；`tier_facts()` 的客服額度說明加入記事簿容量。**程式常數、不放設定表**：AGENTS.md 把它列在會員等級的初始額度（與客服額度同一層），沒有要求可由指令調整；要調整時改常數、走一般發佈。
- **替代方案**：設定表鍵 `rpg.notebook_capacity.<tier>`——沒有營運上的需求，多一組指令與稽核；之後要改只是搬家。

### D4：抓取的執行步驟（沒有任何一步跨模型呼叫持有交易）

```
run(current, plan 的凍結資料, session 的對外識別碼)
 1. 短交易 A：讀存檔（狀態、記事簿文字／版本／標記、世界狀態與主角 → 在場角色年齡）、算邊界 B、
    取視窗的 10 輪（依輪數範圍）、全站預算檢查。決定：
      - 無待抓取（B 不大於標記、存檔已完結）       → 結束
      - 已滿                                        → 短交易推進標記到 B（條件式），結束
      - 全站預算用盡                                → 結束（不推進）
 2. 抓取呼叫：run_structured(kind=rpg_notebook, budget=min(30, 剩餘))
      - 暫時性失敗／預算逾時                        → 結束（不推進）
 3. 驗證：merge_entries（清理、去重）→ 標記與洩漏 → 規則層（D5）
      - 任何條目未通過                              → 推進標記（放棄），結束
 4. 長度：新總長 ≤ 容量＋預備空間？
      - 否且剩餘預算 ≥ 20 秒 → 精簡呼叫一次（回到 3；budget=min(30, 剩餘)）
      - 否且仍超過／預算不足 → 推進標記（放棄），結束
 5. 短交易 B（鎖定存檔列）：條件式 UPDATE 取得鎖（存檔進行中、標記仍小於 B）；
    以鎖內讀到的最新文字重新計算：長度仍 ≤ 容量、追加後 ≤ 容量＋預備空間 → 追加、version+1、標記 = B；
    已被編輯成已滿 → 推進標記（視同已滿跳過）；標記已被別人推進 → 不動作
```

- 視窗以 `turn_number` 範圍取回合（`SessionRepository.turns_in_range`，帶 `user_id`，不提供只憑 ID 的方法），只含 `complete`；呼叫前把玩家行動與劇情原文以 `neutralize` 後放進資料區塊。
- **追加在「最新文字」上**（步驟 5）而不是步驟 1 讀到的文字：抓取期間玩家的編輯與重複觸發都不會被覆蓋，也不需要 `expected_version`。鎖用既有的「對自己的存檔做一次條件式 UPDATE」模式（`start_turn` 已使用）。若鎖內發現長度加上新條目超過上限（玩家在期間貼了很多字），不再要求精簡，直接放棄（推進標記）；這是極少見的競爭，不值得再多一次模型呼叫。
- `LlmService.run_structured` 新增選用參數 `budget_seconds`（預設沿用設定）：精簡重試的預算因此不會使總時間超過 60 秒；總預算 = 60 秒，從步驟 1 開始以 `monotonic` 計。最壞 4 次模型呼叫。

### D5：條目的審查（只用規則層）

條目依序餵給 `rpg_output_rules.evaluate`（段落累積、前一條作為相鄰句；內容模式用存檔的、角色年齡用世界狀態與劇本、主角的），再做 `leak_categories`（標記字串與內部設定）。**結果對應與輸出審查相同**：REJECT、`tier_mismatch`、`minor_suspected`、`real_person_suspected` 擋下，`other_concern` 放行並記錄類別。任何一條被擋就整次放棄，不寫入其他條目。
- **替代方案**：獨立的模型層審查呼叫——已在探索中決定不做（AGENTS.md 第 7 節已改為「模型層用於劇本與主角；記事簿條目只用規則層」）。
- 玩家儲存（D6）用 `check_text_rules` 的 REJECT 類別，與 `GameService.begin_turn` 對行動的處理一致（FLAG 不擋）。

### D6：玩家檢視與儲存

`NotebookService`（建構於請求內，使用請求的工作階段）：
- `view(current, session_id)`：閘門（`require_chat_access`）、擁有者（404）、回傳文字、版本、字數、容量、預備空間、是否已滿。
- `save(current, session_id, text, expected_version)`：閘門 → 擁有者 → `clean_notebook_text`（`None` 視為驗證錯誤）→ 已完結拒絕（`session_ended`）→ 長度大於容量拒絕（`notebook_too_long`，帶字數與容量）→ 規則層（`notebook_blocked`，只帶類別）→ `SessionRepository.save_notebook` 以條件式 UPDATE（進行中且 `notebook_version` 等於預期）→ 成功時版本加 1。條件不成立時重新讀取，已完結回 `session_ended`，否則回 `notebook_conflict` 並附後端最新的文字與版本（比照客服的 `VersionConflict`）。
- 檢查順序讓「已滿時原樣再儲存」被長度規則擋下，且不洩漏規則層命中與否的細節。
- 端點掛在現有的 `game_api_bp`：`GET`／`PUT /api/game/sessions/<id>/notebook`；`GET` 的回應帶 `Cache-Control: no-store`。存檔快照 `session_snapshot` 加 `notebook` 摘要（版本、字數、容量、已滿），不含全文。

### D7：日誌與帳本

`rpg_events` 新增 `log_notebook_captured`、`log_notebook_skipped`（原因：`full`、`empty`、`budget`）、`log_notebook_abandoned`（原因：`blocked`、`too_long`、`stale`）、`log_notebook_failed`（原因：`transient`、`timeout`、`config`、`unavailable`，以及實作時補上的 `validation`〔JSON 或標記字串驗證失敗〕、`blocked`〔供應商擋下〕；皆不推進標記）、`log_notebook_saved`、`log_notebook_save_rejected`（原因：`too_long`、`blocked`、`conflict`、`ended`）。欄位只有內部編號、整數與列舉：條數、最長一條字數、追加字數、總字數、版本、邊界與嘗試次數，**另記「超出提示詞要求」的條數（`over_entries`：超過每次 5 條的部分）與字數（`over_chars`：各條超過 50 字的部分加總）**（皆為整數），供調整提示詞。帳本 `KIND_RPG_NOTEBOOK = "rpg_notebook"`，加入 `LLM_KINDS`；後台 `admin-format.js` 的用途標籤補「記事簿」。

### D8：假模型與驗收

`FakeRpgModel` 依系統指令的固定特徵辨認抓取呼叫（非串流、結構化），預設回 3 條條目；動作文字的關鍵字驅動行為：`測試記事簿超量`（條目超過預備空間，第二次精簡後符合）、`測試記事簿放棄`（精簡後仍超過）、`測試記事簿違規`（條目命中規則層）、`測試記事簿慢速`（延遲數秒，驗證串流先結束）。HTTP 驗收需要 11 輪（330 幣），沿用小幫手建立全新玩家並補幣（實作時確認）。容量驗收對 `gunicorn.rpg.conf.py` 的容器實測 D1 的前提。

## Risks / Trade-offs

- **[`call_on_close` 在 gthread 上的時序不如預期（結束 chunk 在抓取之後才送出，或斷線時不呼叫）]** → 容器驗收有明確的檢查（串流在 `done` 之後很快結束、斷線後記事簿仍被更新）；不成立時改為在產生器的 `done` 之後呼叫同一個可呼叫物件（D1 的退路）。
- **[抓取期間佔用一條 gunicorn 執行緒，最長 60 秒]** → 抓取只在每 10 輪發生一次；RC-4 估容量時計入；`gunicorn.rpg.conf.py` 的逾時 180 秒足以承接。
- **[免費模型常寫超過 5 條或 50 字，免費記事簿比預期早滿]** → 這是決定 3 的取捨（不丟棄、不截斷）；日誌記整數，真實模型驗證時量測，必要時只調整提示詞，不改規則。
- **[抓取失敗反覆發生，每個回合都重打模型]** → 失敗不推進標記所以下個回合會再試；但每次回合完成最多一次抓取（最壞 4 次呼叫）、計入全站預算、受全站上限約束。若觀察到成本問題，之後再加冷卻（需要新欄位，屬後續 change）。
- **[玩家把違規設定寫進記事簿]** → 儲存時規則層擋拒絕類別；資料區塊 `neutralize`；輸出仍逐句審查。規則層詞表的涵蓋不是完美的，這與劇本、主角相同，列入既有的法務覆核。
- **[競爭：玩家快速送出下一輪使兩個抓取重疊]** → D4 步驟 5 在鎖內以標記判斷，只有一次追加生效；最壞多花一次模型呼叫，不會有重複條目。
- **[抓取的輸入是玩家行動與劇情原文，會傳送到 RPG 模型供應商]** → 與回合本身相同的資料流，AGENTS.md 第 11 節已涵蓋；免費模型期間不得輸入真實個資的限制不變。
- **[worker 在抓取期間被關閉]** → 標記沒有推進，由下一個回合補做；不會留下半寫入的狀態（寫入只有一個短交易）。

## Migration Plan

- 無資料庫遷移；`llm_call_logs.kind` 是自由字串，新值直接可用。部署照一般流程（新版程式上線即可）。
- 回滾：還原程式即可。既有的記事簿內容與玩家編輯保留在欄位中，上下文仍會讀取；`notebook_upto_turn` 不再被推進，若之後重新部署，補做的視窗依「舊視窗放棄」規則只抓最新的一個。

## Open Questions

- 玩家儲存端點是否需要額外的限流（目前只有入口層的一般限流，內容最長 10000 字）。不影響規格與任務，觀察後再議。
- `tier_facts()` 加入記事簿容量後，客服對話的既有測試若固定了整段文字，apply 時一併更新。
