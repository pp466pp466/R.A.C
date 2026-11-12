# Design

## Context

動機與範圍見 `proposal.md`；行為契約見 `specs/`。這份只講怎麼做與為什麼。規範來源：AGENTS.md 第 6、7、10、11 節（RA-17 已寫入）、`docs/RPG.md` 7.8；本次探索（2026-11-12）的結論在 `HANDOFF.md`。

現況（已讀過程式）：

- **回合列** `memory_logs`（`app/models/rpg_session.py`）：`turn_status`（`streaming`／`complete`／`interrupted`／`blocked`）、`attempt`、`seq`、`turn_number`（只有 `complete` 才有）、`user_action`、`llm_raw_response`、`structured_result`。`(session_id, client_msg_id)` 與 `(session_id, seq)` 唯一。
- **回合生命週期**：`GameService.begin_turn`（請求內：閘門、驗證、規則層、預算、`start_turn`＋`wallet.hold` 在同一個 SAVEPOINT、凍結 `TurnPlan`）→ `TurnRunner.events()`（產生器內自己管短交易：串流、`OutputGuard` 逐句審查、被擋自動重生一次、狀態呼叫 `_state_phase`、`finalize` 以 `finish_turn` 完成並結算）。失敗路徑（`_interrupt`／`_record_blocked`）把回合改為 `interrupted`／`blocked` 並退款。`start_turn` 取得存檔列鎖、檢查「同時只有一個未逾時的 `streaming`」。
- **世界狀態**：`sessions` 的 `current_location`、`status_summary`、`entities_json`、`goals_json`、`milestone_progress_json`、`elapsed_months`（`WORLD_STATE_FIELDS`），以 `version` 做條件式更新；`rpg_world.WorldState.from_fields/to_fields` 已能與這六欄互轉。
- **記事簿**：`notebook_policy.window_for(completed)` 的抓取視窗是 `⌊(完成輪數−1)÷10⌋×10` 往前 10 輪——**永遠不含最後一輪**（第 10 輪完成時視窗為空，第 11 輪完成時才抓第 1 至 10 輪）。
- **錢包**：`hold` 對同一個鍵一律回傳既有預扣（包含已退回的），所以鍵必須含 `attempt`（`turn:{存檔}:{client_msg_id}:{attempt}`，`docs/RPG.md` 7.15.2）；預扣逾時秒數 `coin.hold_timeout_seconds`（預設 180）現讀自設定表。
- **前端**：`game-session.js` 管串流與時間軸，`game-sync.js` 的 `catchUp` 以本機最大序號為 `since_seq`（只往後讀）、`resolveInflight` 用「本機已有該 `client_msg_id` 的 `complete` 回合」判定在途回合已完成，`game-store.js` 的在途紀錄每存檔一筆。名稱約定在 `tests/js/support/game-contract.js`、`session.html` 標頭、`game.js` 回傳三處。

## Goals / Non-Goals

**Goals：**

- 最後一輪可被重新生成或編輯，且**失敗時什麼都不變**：內容、世界狀態、免費次數、餘額、記事簿都回到操作之前。
- 兩個裝置、重複請求、斷線、worker 重啟任何組合下，不會扣兩次幣、不會留下半替換的狀態，也不會讓退回過的預扣鍵被撿回。
- 重用現有的串流、輸出審查、被擋處理與狀態驗證，不另寫一套；既有回合的行為與測試不變。
- 理由只供營運檢視：不進模型、不進日誌、不進 IndexedDB。

**Non-Goals：**

- 管理員的回饋檢視頁與統計（後續小 change）；第 0 幕的重新生成；多輪回溯；把普通回合的斷線計入免費次數；ledger 新增專屬的帳本種類；提示詞與審查規則的任何調整。

## Decisions

### D1 「最後一輪」與認領：回合列保持 `complete`，另用認領時間表示修訂中

回合在重新生成期間仍是 `complete`（舊內容必須一直可讀，也讓「輪數／上下文」不受影響）。新增欄位：

| 欄位 | 型別 | 意義 |
|---|---|---|
| `revision` | int，預設 1 | 每次成功修訂加 1；前端用它偵測並行與覆寫快取 |
| `regen_count` | int，預設 0 | 目前這份輸入已成功重新生成幾次；成功的重新生成加 1，成功的編輯歸零 |
| `revising_started_at` | datetime，可為空 | 認領時間；空或逾時＝沒有人在修訂 |
| `state_before_json` | JSON，可為空 | 回合前快照（見 D4） |

**可修訂**＝同時成立：存檔 `active`；該回合是存檔最後一個 `complete`；其後沒有未逾時的 `streaming` 回合（被擋、中斷、逾時的 `streaming` 都不算）；`state_before_json` 不為空；沒有未逾時的認領。不成立時依序回 `ended`、`not_last`、`in_progress`、`no_snapshot` 原因的 409 `not_revisable`。

**認領**是單一條件式 UPDATE（沿用 `start_turn` 取得存檔列鎖的做法，序列化同一存檔的並行請求）：

```
UPDATE memory_logs SET revising_started_at = :now, attempt = attempt + 1
WHERE id = :id AND turn_status = 'complete' AND revision = :expected
  AND (revising_started_at IS NULL OR revising_started_at < :cutoff)
```

rowcount 0 時再讀一次區分 `revision_conflict`（revision 已變）與 `turn_in_progress`（認領未逾時）。`cutoff` ＝ 現在減 `coin.hold_timeout_seconds`（現讀），所以認領與預扣同時到期。`start_turn` 的 `_ensure_no_other_streaming` 一併檢查「同存檔是否有未逾時的認領」（→ `turn_in_progress`）。

- 備選：把回合改成新的狀態 `revising`。拒絕：所有「只有 `complete` 才計入輪數／上下文／重播」的查詢都要改，且舊內容在修訂期間讀不到；欄位法只動新程式。
- 備選：重新生成寫成新回合列、舊列標 `superseded`。拒絕：序號、輪數、`client_msg_id` 的唯一與連續性都會受影響，前端快取也要處理刪除。

### D2 端點、請求與錯誤對照

兩個路徑都在 `game_api_bp` 之下，`<client_msg_id>` 是路徑參數（不是本文），同一個回合永遠是同一個 `client_msg_id`：

| 端點 | 本文 | 開始前的檢查順序 |
|---|---|---|
| `…/turns/<client_msg_id>/regenerate/stream` | `expected_revision`、`category`、`reason` | 閘門 → 驗證本文 → 擁有者與回合（404）→ `revision`（409 `revision_conflict`）→ 可修訂（409 `not_revisable`）→ 全站預算（503 `system_busy`）→ 認領＋（付費者）預扣＋回饋列，同一個 SAVEPOINT |
| `…/turns/<client_msg_id>/edit/stream` | `expected_revision`、`action` | 同上，驗證本文時改用 `clean_action`，並加規則層（422 `input_blocked`）與「與原行動不同」（422） |

錯誤代碼只用既有加上兩個新的：`revision_conflict`、`not_revisable`（皆 409，繁體中文訊息；`not_revisable` 帶 `reason` 列舉）；其餘沿用 `turn_in_progress`、`insufficient_coins`、`system_busy`、`input_blocked`、validation。`insufficient_coins` 時整個 SAVEPOINT 復原，**不留認領、不留回饋列**。

- **為什麼編輯不換新的 `client_msg_id`**（修正 `docs/RPG.md` 7.17.7 ⑤ 的舊想法）：⑤ 的前提是編輯走 `turns/stream`，而該端點對「相同 `client_msg_id`、不同行動」會重播既存結果；現在編輯是獨立端點，冪等由 `expected_revision`＋認領保證——重複的請求得到 409，不會重複扣款。回合的 `client_msg_id`、`seq`、輪數因此永遠不變，前端的快取鍵、在途紀錄與重播都不必處理編號改變。
- **為什麼 `reason` 驗證放在 Service**：長度以 NFC 與去除控制字元之後的 Unicode 字元計（5 至 50，與記事簿的計法同一個 `length_of` 規則），常數（5、50）與五個分類列舉放 Service 模組，前端從頁面初始狀態讀（不寫死），測試固定。

### D3 費用、免費次數與預扣鍵

- **免費次數**＝設定表 `rpg.free_regenerations`（RPG 組，0 至 20，初始 2，遷移寫入，每次現讀，`flask set-rpg-setting` 修改並稽核）。認領時：`regen_count < 免費次數` 才免費。因為認領互斥，不會有兩個請求同時搶到最後一次免費。
- **何時算用掉**：成功替換時 `regen_count + 1`（編輯則歸零）。失敗、被擋、中斷不動它。（決定 4：斷線時若文字已完整、收尾完成就算用掉，否則不算——與 `TurnRunner._on_close` 的規則一致。）
- **付費**：重新生成超過免費次數、以及所有編輯，呼叫 `wallet.hold(key=turn:{存檔}:{client_msg_id}:{attempt}, model_key="standard")`，`attempt` 是認領後的新值。`attempt` 從不減少，所以：原回合的鍵（attempt 1）與修訂的鍵不同；退回過的修訂鍵不會被下一次撿回；逾時被接手的新認領用更新的鍵。付費與免費重新生成使用同一套鍵格式，免費者只是不呼叫 `hold`。
- **結算／退回**：成功→`settle`；失敗、被擋、中斷、被接手→`refund`（已有的 `HoldAlreadyRefunded` 容忍規則照用）。沒有預扣的免費重新生成，失敗時只釋放認領。
- 帳本種類沿用既有的預扣（顯示為「冒險回合」）；新增專屬種類要改 W1 的錢包契約與記錄頁，不在此 change（見 Open Questions）。

### D4 回合前快照與回滾：「先還原再套用」

- **存**：`begin_turn` 建立回合列時（`start_turn` 成功之後、同一個交易內）把 `WorldState.from_fields(存檔六欄).to_fields()` 寫入新回合的 `state_before_json`；被擋／中斷後以同一個編號重送時重新覆寫（`_resend` 路徑）。大小受世界狀態既有上限約束。
- **清**：`finish_turn` 完成一個**新**回合時，在同一個交易內把「前一個完成回合」的 `state_before_json` 設為空（條件式 UPDATE，`turn_number = 新輪數 − 1`）。被擋與中斷的下一輪不清。結果：全存檔最多一個回合有快照；沒有快照的回合（遷移前、或已被清除）就是 `no_snapshot`。
- **用**：修訂的 `TurnPlan.world` ＝快照（不是目前存檔），所以上下文、NPC 切片、`apply_proposal`、`compute_ending_due` 都以「那一輪開始之前」的世界為基礎；寫回時 `to_fields()` 的結果取代六欄，以認領時的存檔 `version` 比對（版本加 1）。原本那輪造成的地點、目標、NPC 弧線變化因此自然被覆蓋，不需要逐欄反向運算。
- **版本不一致**＝整個修訂失敗（保留舊內容、退回），不是「替換文字但不寫世界狀態」：那會讓文字與世界狀態對不上（`TurnRunner` 的普通回合可以容忍，是因為文字已送達且不會被收回）。
- 備選：只存「差異」並反向套用。拒絕：`apply_proposal` 的結果含年齡更正、錨定事實等巢狀變更，反向運算容易漏；整份快照簡單且可驗證（回合前＝快照，回合後＝快照套用提議）。

### D5 上下文：不含目標回合的歷史

`build_turn_context` 已吃 `history`、`turn_number`、`opening_text`。修訂傳入：`history`＝「輪數小於目標輪數的最近 10 個完成回合」（`SessionRepository.complete_turns_before(user_id, public_id, turn_number, limit=10)`，新方法，簽名帶 `user_id`）、`turn_number`＝目標回合的輪數、`action`＝原行動或新行動、`notebook`＝目前的記事簿（只讀）。記事簿包含的是更早視窗的條目，與目標回合無關（見 Context），所以不需要回滾也不需要重算。第 0 幕的條件（`turn_number − 1 < 10`）照舊成立。

### D6 `RevisionRunner`：共用串流與狀態階段，收尾另寫

`TurnRunner` 的收尾（`finish_turn` 指派新輪數）與失敗路徑（改 `blocked`／`interrupted`）都不適用修訂：修訂要「保留 `complete`、只退款與釋放認領」，成功用 `replace_turn`。做法：

1. 把 `TurnRunner` 的 `_StateOutcome`、`_state_event`、`_state_phase` **原樣搬到共用模組**（`rpg_state_phase.py`），`TurnRunner` 改為呼叫它（純重構，既有測試與日誌必須逐位元不變）。
2. 新增 `RevisionRunner(RunnerBase)`：`_attempt`（串流＋逐句審查）、被擋自動重生一次（`transition` 事件與文字沿用）、共用的狀態階段，差別只有：
   - 被擋／失敗／中斷：`release_revision(attempt)`（條件式 UPDATE 清認領）＋退款＋回饋列標 `blocked`／`failed`；舊內容不動、**被擋的文字不保存**（回合列存的是現行內容，沒有地方放；也不需要）。
   - 狀態階段 `ok=False`（呼叫失敗、驗證失敗）＝修訂失敗（決定 3；AGENTS.md「通過審查與狀態驗證才替換」）。
   - 成功：單一短交易呼叫 `replace_turn`＋`wallet.settle`＋回饋列標 `applied`＋（提議結局時）`end_session`。
   - `GeneratorExit`：文字已完整（`_narration` 有值）則收尾，否則釋放認領並退款（與 `_on_close` 相同形狀）。
3. `replace_turn(user_id, public_id, client_msg_id, *, expected_revision, attempt, llm_raw_response, structured_result, user_action=None, regen_count_delta/reset, world_changes, expected_version, end_story)`：一個條件式 UPDATE `WHERE id AND turn_status='complete' AND revision=:expected AND attempt=:attempt AND revising_started_at IS NOT NULL`，`SET revision=revision+1, revising_started_at=NULL, llm_raw_response=…, structured_result=…`；rowcount≠1 拋出 `InvalidTurnTransitionError`（→ `turn_superseded`）。世界狀態用既有 `update_world_state`（版本比對失敗 → 整個交易復原，由 Runner 轉為修訂失敗）。

- 備選：讓 `TurnRunner` 加一個 `mode`。拒絕：收尾與失敗路徑的分支會讓一個本來單純的類別變成兩套邏輯交錯，且回合的既有測試很難證明沒被影響。

### D7 回饋表 `turn_regenerations`

| 欄位 | 說明 |
|---|---|
| `id`、`created_at`、`updated_at` | 主鍵與時間 |
| `session_id` | 外鍵，`ON DELETE CASCADE`（隨存檔與帳號刪除） |
| `turn_seq` | 回合序號（不用內部回合編號，回合列不對外） |
| `attempt` | 嘗試序號；`(session_id, turn_seq, attempt)` 唯一 |
| `kind` | `regenerate` 或 `edit` |
| `category` | 五個分類之一；編輯為空 |
| `reason` | `UnicodeText`，5 至 50 字；編輯為空 |
| `is_free` | 布林 |
| `result` | `pending`（預設）、`applied`、`failed`、`blocked` |

認領的同一個 SAVEPOINT 內插入（`pending`）；收尾時標結果；被接手時先把同回合仍為 `pending` 的舊列標 `failed`。**不存**被取代的行動與劇情（資料最小化，7.8）。不篩選 JSON、無 Raw SQL。個資：隨存檔／帳號刪除、納入匯出（`personal_export_service`）、日誌只記內部編號與整數長度；管理員檢視頁不在此 change，因此**沒有任何管理員讀取路徑**，也沒有新的稽核需求。

### D8 玩家端的資料：`revision` 與 `last_turn`

- `turn_view` 加 `revision`；`done` 事件加 `revision` 與 `free_left`（見 spec）；重送事件同步。
- `session_snapshot` 加 `last_turn`（見 `rpg-sessions` delta）：`revisable` 與原因由與 D1 相同的函式計算，避免畫面與端點判斷不一致。幣價取自 `RuntimeLimits`（現讀）。這讓前端不必自己推論「哪一輪是最後一輪、免費還剩幾次」。
- 初始狀態（頁面控制器）加 `revision_limits: {reason_min: 5, reason_max: 50, categories: […]}`，前端不寫死數字。

### D9 前端

新增 `app/static/js/game/game-revision.js`（UMD，`createRevision(deps)`，依賴注入同 `createNotebook`）：

- **狀態**：`revisionBusy`、`regenerateModal`（`open`、`category`、`reason`）、`editModal`（`open`、`draft`）、`lastTurn`（取自快照與 `done`）；全由 Vue 接管，名稱約定同步 `game-contract.js`、`session.html` 標頭與 `game.js` 回傳三處。
- **流程**：開啟 Modal → 驗證（沿用 `actionProblem`；理由以 `[...text.normalize('NFC')].length` 計 5 至 50）→ 先寫在途紀錄（`kind`、`expected_revision`、編輯另含新行動；**不含理由與分類**）→ `TurnStream.run`（重用既有串流客戶端，換網址與本文）→ 事件處理：新文字進最後一輪的氣泡（舊內容留在記憶體）→ `finally`：不論結果，`catchUp` 從「最後一個完成回合的前一個序號」重抓並覆寫、恢復按鈕、清在途紀錄。
- **失敗還原**：`done=blocked`、`error`、逾時、中斷、取消→還原舊內容並以 Toast 說明「已保留原本的回應，沒有扣遊戲幣、沒有用掉免費次數」；成功→以後端內容覆寫。
- **快取**：`game-sync.js` 的 `catchUp` 新增 `fromSeq` 選項（預設仍是本機最大序號，既有呼叫不變）；`sameTurn` 比較納入 `revision`；`resolveInflight` 對 `kind != 'turn'` 的紀錄改用 `revision > expected_revision` 判定成功，兩種結果都清除紀錄、都不自動重送，沒完成時發出「上次沒有完成」通知。`game-store.js` 的 `inflightRecord` 白名單加 `kind`、`expected_revision`（理由與分類不在白名單，由 `game-store` 測試固定）。
- **介面**：按鈕在最後一輪氣泡下方；Modal 沿用現有電影感 Modal 與 `UI.Modal`／Toast；餘額不足沿用既有的 `insufficient_coins` 簽到 Modal；全部純文字、無 `data-bs-*`、無 `v-html`。

### D10 日誌、遷移與政策頁

- **日誌**（`rpg_events`，白名單）：`rpg.revision_started`（kind、attempt、is_free、cost）、`rpg.revision_completed`（kind、attempt、字數、延遲、`world_updated`、`ended`）、`rpg.revision_failed`（kind、attempt、原因列舉：`blocked`、`timeout`、`model_failed`、`state_failed`、`superseded`、`disconnected`、`internal`）。只有內部編號、列舉與整數；被擋沿用 `log_turn_blocked` 的類別。
- **遷移 0018**（expand）：`memory_logs` 加 `revision`（NOT NULL、`server_default` 1）、`regen_count`（NOT NULL、`server_default` 0）、`revising_started_at`（可為空）、`state_before_json`（可為空）；新表 `turn_regenerations`；設定表新增 `rpg.free_regenerations`＝2（以 ORM／Alembic op API，不用原生 SQL）。`recreate="never"` 的做法同 0016、0017。遷移不回填舊資料列的快照。
- **政策頁**：`/privacy` 增列理由回饋（用途、不送 AI、保存、刪除、匯出）與遊戲幣帳本（目前缺）；`/terms` 增列規則；`CURRENT_TERMS_VERSION` 提高一版（只影響之後新註冊者記錄的版本，不強迫既有使用者重新同意）。AGENTS.md 不改。

## Risks / Trade-offs

- **重新生成可被當成「抽卡」** → 前兩次免費、之後收費、理由必填且保存供營運檢視；`rpg.free_regenerations` 現讀可即時調整；中斷的普通回合仍可免費重來，列為觀察項（7.16.4），不在此處處理。
- **快照讓每個新回合多寫一份世界狀態 JSON**（寫入量增加、最多保留一份）→ 世界狀態本來就有大小上限；下一輪完成即清除；若 RC-4 的負載測試顯示壓力，快照可改存壓縮或差異，介面不變。
- **認領後 worker 死掉**→ 認領逾時（與預扣相同）後可被接手，舊預扣由逾時規則退回；回饋列在接手時標 `failed`。逾時前玩家看到「處理中」，最長約 3 分鐘。
- **重構 `_state_phase` 可能改變普通回合的行為** → 搬移前後以既有的 `rpg_turn_runner` 測試與日誌掃描逐項比對；搬移單獨成為一個任務與一個驗證點。
- **修訂結果與記事簿**：邊界輪（第 10、20…）被修訂後才被抓取，是設計預期；若玩家在抓取後的下一輪之前就修訂，視窗不含它，沒有影響（Context）。
- **前端中止不代表後端中止**：玩家取消後後端仍可能替換成功；重抓覆寫快取，Toast 不能說「沒有修改」，只說「以伺服器上的內容為準」。
- **既有回合沒有快照**：遷移前建立的回合不可修訂；影響範圍只有開發與驗收資料。

## Migration Plan

1. 備份 → 執行 0018（只新增，可為空或有預設）。
2. 部署新版程式；舊版程式在新結構上可運作（不認識新欄位與新表）。
3. 回復（downgrade）移除 4 個欄位、新表與設定列，只在沒有真實資料時使用，有資料時以備份還原。
4. 開發 SQL Server `project` 目前在 0017，驗收環境照 `HANDOFF.md` 的做法重啟（不加 `-Reset`）。

## Open Questions

- 帳本是否要為修訂另立種類（例如「重新生成」）讓記錄頁分得出來？預設：先沿用「冒險回合」，`player-hub` 做記錄頁時再決定（改種類是 W1 錢包契約的變動，影響範圍超出此 change）。
- 回饋的分類名稱與順序是 `docs/RPG.md` 7.8 的暫定五類；正式文案在實作時與使用者確認一次，不影響資料結構（`category` 是列舉字串，增減由 Service 常數與遷移外的程式處理）。
