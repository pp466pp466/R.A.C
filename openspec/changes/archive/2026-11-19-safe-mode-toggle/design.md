# Design

## Context

動機見 `proposal.md`。現況（2026-11-19 讀程式所得，**觀察**）：

- 三值的內容模式 `safe／standard／adult` 散在 `app/models/user.py`（`CONTENT_TIERS`）、`app/services/content_tier.py`、`rpg_prompt.py` 的 `TIER_BOUNDARIES`、`script_rules.py`（`tier == STANDARD and saw_explicit` 等分支）、`rpg_output_guard.py`、`script_spec.py`、`script_review_prompt.py`、前端 `game-labels.js`／`script-draft.js` 的標籤表。
- 生效模式由 `AccessPolicy.effective_tier(user, script_tier=…, save_tier=…)` 取玩家偏好、年齡上限、劇本宣告、**存檔記錄**四者最保守；`game_service.py` 三處（開局、重生、第 0 幕）傳入 `save.content_tier`，所以存檔等於把開檔當下的模式釘住。
- 成人模式的可用性由 `ADULT_MODE_ENABLED`＋`ADULT_MODE_MODELS`（`app/config.py`）與 `ContentSettings` 決定；`ContentModeService.set_mode` 要 `confirm_adult` 與 `confirm_rules` 兩個勾。
- 目錄在 `script_repository._published_window` 以 `content_tier.in_(…)` 篩選，`startable.py` 對高於玩家模式的劇本回 403。
- `PUT /api/account/content-mode` 沒有任何前端呼叫；`/account` 沒有安全模式區塊。
- 串流的 `state` 事件已有 `notices` 欄位（`rpg_turn_runner.py`），可承載提示而不新增事件種類。
- 唯一的內建劇本 `xianxia-qingyun.json` 是 `safe`。

**這份設計沿用的、使用者已確認的決定**（2026-11-19）：做完整版；存檔跟著玩家設定、不釘住；六項建議預設全數採用（新劇本預設未綁定、緊急煞車用新環境變數、沿用 `adult_*` 同意欄位、提示每存檔一次、安全模式下不自動調高、expand/contract 相容舊值 `standard`）；展示用劇本採「改內建劇本為未綁定」。

## Goals / Non-Goals

**Goals:**
- 兩種模式、生效模式現算，單一入口、全站一致（Service、審查、提示詞、前端顯示都吃同一個結果）。
- 玩家能在介面切換安全模式；劇本可綁定；目錄通知與遊戲內提示到位。
- 11-23 前可部署到展示環境，且全程可回復（備份、快照、舊映像標籤）。

**Non-Goals:**
- 不改四類絕對禁止項、不改審查詞表與句級緩衝機制本身。
- 不引入付費模型、不加強年齡驗證（維持自行聲明）。
- 不在這個 change 做 contract：`sessions.content_tier`、`users.is_adult_at_confirmation` 欄位保留不刪；既有資料的 `standard` 不改寫（讀取時正規化即可）。
- 不新增事件種類、不新增模型呼叫（提示由規則層與審查層判定）。

## Decisions

### D1. 內容模式只有 `safe`／`adult`；`standard` 在讀取邊界正規化為 `adult`
`parse_tier` 對 `standard` 回 `adult`；新增兩值的 `CONTENT_MODES`；三值的 `CONTENT_TIERS` 在批 A 期間作為**過渡相容層**暫時保留（`effective_tier(save_tier=…)`、`content_access`、`author_content_cap` 亦同），讓每個 commit 的測試皆為綠，待第 2 組（目錄、開存檔、審查改完）才縮減與移除，並以守門測試鎖住。所有「從資料庫或劇本內容讀出模式」的地方（玩家偏好、`script_versions.content_tier`、`sessions.content_tier`、種子與草稿內容）一律經 `parse_tier`，所以**不需要改寫既有資料**，混合版本部署期間新舊程式都能運作。
替代方案：以遷移把 `standard` 改寫為 `adult`——需要資料改寫與回填，且已發佈的劇本版本「不可變」，改寫它們與規範相衝，故不採用。

### D2. 生效模式由 `AccessPolicy` 單一函式現算：`effective_mode(user, script_binding=None)`
結果 ＝ `most_conservative(玩家偏好（缺少目前版本的同意視為 safe）, script_binding（綁定為 safe，未綁定或無劇本不參與）, 緊急煞車（啟用為 safe）)`。移除年齡上限分支（`content_access` 的 `is_adult`、`author_content_cap`）與 `save_tier` 參數。`game_service` 的三處改為傳入存檔複本裡劇本的綁定（`script_snapshot_json` 的 `content_tier`，經 `parse_tier`），所以劇本日後的下架或改版不影響進行中的存檔，而玩家設定的改變於下一回合起生效。
`sessions.content_tier` 欄位仍在開存檔時寫入（值為開檔當下的現算結果，供新舊版程式同時存在時的舊程式讀取），但**新程式不讀它**；我的存檔清單與遊戲頁的內容模式一律現算。欄位在日後的 contract change 才移除。
替代方案：存檔旁另存「釘住的模式」——使用者已決定存檔跟著設定，故不採用。

### D3. 同意紀錄沿用 `users.adult_confirmed_at`／`adult_rules_version`／`adult_confirm_method`
不改欄位名（免一次 contract）；常數改名 `SAFE_RULES_VERSION`（值換新，例如 `2026-11-19-draft-2`），所有舊同意因版本不符而失效，玩家下次關閉時重新同意。`ContentModeService.set_mode` 只接受 `mode ∈ {safe, adult}`，`adult` 要求已同意目前版本或本次請求的 `confirm_rules is True`；不再讀 `confirm_adult`。已確認後再次關閉不重勾（延續既有規格）。UI 在已同意時直接送出請求，不顯示 Modal（假設；若使用者想每次都確認，只需改前端）。
回應形狀固定為 `{preference, effective, confirmed, forced}`。

### D4. 緊急煞車 `SAFE_MODE_FORCED`，取代 `ADULT_MODE_ENABLED`／`ADULT_MODE_MODELS`
`ContentSettings` 改為只含 `safe_mode_forced: bool`（`_bool_setting`，值不是布林啟動失敗，訊息只含變數名）；預設 `False`。`AccessPolicy` 以建構參數收到它。`ADULT_MODE_ENABLED`、`ADULT_MODE_MODELS` 與兩個 `SEXUAL_CONTENT_LEVEL3_*` 一律「被忽略並於啟動時警告變數名稱」（延用既有的淘汰警告機制）；`.env.example` 與 `scripts/launch_plan.py` 同步（計畫檔含舊變數被拒、可含 `SAFE_MODE_FORCED`）。緊急煞車啟用時 `PUT` 要求 `adult` 回 403 `safe_mode_forced`，要求 `safe` 仍可成功。
**設定位置**：展示環境的環境檔由 `aws_demo.py` 管理；`SAFE_MODE_FORCED` 要能經既有的「重放主機檔案」路徑設定（不需改映像），並在營運文件寫下「出事時如何啟用與解除」。

### D5. 目錄與開存檔不再依模式過濾或拒絕
`script_repository` 的 `_published_window` 與按標籤查詢移除 `max_tier` 參數與 `content_tier.in_(…)`；`startable.py`、`session_service`、創角資料與主角生成不再比較劇本與玩家模式，移除 `ContentTierRestricted`（及其 403 `content_tier_restricted`）、`ContentModeRestricted`、`AdultModeUnavailable`，新增 `SafeModeForced`。卡片欄位 `content_tier` 保留並經 `parse_tier` 輸出（`safe`＝綁定、`adult`＝未綁定）。目錄回應的 `content_mode` 改為 `{effective, forced}`（不含 `is_adult`）。

### D6. 劇本綁定沿用 `content_tier` 欄位，不新增布林欄位
`script_spec.validate_script` 接受 `safe`／`adult`／舊值 `standard`（標準化為 `adult`），不再有 `max_tier` 參數；`content_hash` 因標準化而與舊版不同的風險：只對**新儲存**的內容標準化，既有版本不重算雜湊（避免觸發「內容改變」）。審查：`script_rules` 與 `script_review_prompt` 的 `tier_mismatch` 只在綁定（`safe`）時檢查性暗示與露骨；未綁定的劇本不因性內容產生 `tier_mismatch`，未成年角色加性相關詞維持 `flag minor_suspected`（原 `standard`／`adult` 的行為）。創作頁以「綁定安全模式」勾選框取代下拉：`script-draft.js` 的 `TIER_ORDER`／`allowedTiers` 移除，`toDraft` 把 `standard` 讀成 `adult`。
替代方案：新增 `safe_bound` 布林——要改固定規格版本、雜湊與遷移，而語意與「`safe`＝綁定」完全相同，故不採用。

### D7. 審查與提示詞只有兩段尺度
`rpg_prompt.TIER_BOUNDARIES` 刪除 `standard` 段，`safe` 段不變（沿用原安全模式的嚴格尺度），`adult` 段不變。`check_text_rules` 刪除 `tier == STANDARD` 的分支：`safe` 偵測到性暗示或露骨為 `tier_mismatch`，`adult` 不因性內容產生 `tier_mismatch`；四類絕對禁止項、疑似未成年與真人名義不論模式一律執行。`rpg_output_guard` 以 `parse_tier` 取得模式，行為隨之。

### D8. 遊戲內提示：伺服器判定、`state` 事件的 `notices` 承載、資料庫條件式更新保證一次
- **判定**（Service）：玩家設定為安全開且已排除綁定與緊急煞車的存檔，才進入判定；輸入以既有的 `mentions_sexual`（`protagonist.py` 所用的詞表判定），輸出以審查層是否因性內容擋下或降為安全尺度（回合結果已有被擋類別與自動重生的資訊）。**劇本綁定時不提示**（關閉安全模式對該劇本沒有作用，提示會誤導）。
- **記錄**：`sessions.safe_hint_at`（`UTCDateTime`、可為空，遷移 0020 新增；ORM 以 `update … where safe_hint_at is null` 的條件式更新搶先記錄，更新列數為 1 才送出提示，多 worker 與重送都不會重複）。只在回合**成功完成**時嘗試；被擋、中斷、重生被取代的回合不記錄。
- **送達**：`state` 事件的 `notices` 加一個固定列舉項 `safe_mode_hint`（不含任何文字，文字由前端 `game-labels.js` 的固定繁體中文提供，不來自模型）。前端以現有通知機制顯示，帶「前往帳號設定」連結（返回位置為該存檔頁）與關閉鈕；關閉只影響畫面。
- **日誌**：`rpg_events` 新增事件，只含事件名稱、使用者與存檔的內部編號與原因列舉（`input` 或 `output`）。
替代方案：每次都提示（太吵）；以模型輸出判斷（會多一次呼叫且不可預測）；寫入前端 localStorage 控制次數（換裝置會重複）——皆不採用。

### D9. 返回位置：`/account?return=…`
Page Controller 與前端各自以同一份規則驗證：必須以 `/game/` 開頭、不含 `//`、反斜線與控制字元、長度上限 200；驗證通過才放進初始狀態，其餘丟棄（開放轉址防護）。「完成並返回」是一般連結；成功修改後前端直接導向（`location.assign`）。目錄的詢問 Modal 與遊戲內提示都使用同一個 `buildAccountLink(returnPath)`。

### D10. 前端結構
- `/account`：`auth/account.html` 新增 `account-safe-mode` 區塊（Vue 全接管；Modal 使用共用 `ui-modal`），邏輯放新檔 `app/static/js/auth/account-safe-mode.js`（狀態機＋純函式，沙盒可測），`account.js` 只接線；安全規範的重點文字與兩種狀態的說明放在 `account-safe-mode.js` 的固定文案常數（全部本機、繁體中文、純文字渲染）。
- `/game`：`entry.js` 增加「點選未綁定劇本的詢問」狀態（`pendingStart`）；`game-labels.js` 的 `TIER_LABELS`、`contentModeNotice`、`GATE_FAILURES.content_mode_restricted` 與 `MINOR_NOTE` 改寫或移除；`game-session.js` 顯示現算模式與提示。
- `/scripts`：`script-draft.js`、`script-workshop.js`、`workshop.html` 換成勾選框。
- 不新增 `data-bs-*`，所有 Modal 以 `v-if` 接管；樣式沿用既有 `ui-modal` 與 Toast。

### D11. 內建劇本改為未綁定
`app/data/scripts/xianxia-qingyun.json` 的 `content_tier` 由 `safe` 改為 `adult`（內容本身不動）；部署時執行 `flask seed-scripts`（人工或部署流程，不在啟動時自動執行）新增第 2 版取代第 1 版，已開的存檔不受影響（既有「內容改變時新增版本取代舊版」規則）。註：這使安全模式開的玩家選這個劇本時**會**出現目錄詢問，是展示的預期行為。

### D12. 條款與隱私權政策
`/terms` 補上露骨內容、安全模式兩種狀態、絕對禁止項、18 歲與謊報責任、供應商留存；`/privacy` 補上關閉安全模式時內容可能被 RPG 模型供應商留存或人工審閱；條款版本提高（`terms_version` 常數換新）。`/terms` 第 5 節「成年使用者預設使用一般模式」一句一併更正（`adult-only-signup` 已記下此待辦）。仍標示為草稿，法務覆核屬 `launch-gates`。

### D13. 遷移 0020（expand）
只新增 `sessions.safe_hint_at`（可為空，不建索引、不設預設）；`recreate="never"` 的 downgrade 比照 0019；`docs/project資料結構文件.md` 同步並於第 14 節追加異動紀錄；`tests/test_project_db_schema_doc.py` 守門。部署順序：備份與 RDS 快照 → 遷移 → 新版程式 → `seed-scripts` → 驗證；回復：兩個池換回舊映像（`aws_demo.py rollback`），舊程式在新結構上可運作（新欄位可為空且不被讀）。

## Risks / Trade-offs

- **測試與文件中三值常數很多**（138 處、43 個檔案）→ 先改核心（D1、D2）讓測試大量失敗，再依 Service、前端、文件分批修，每批 commit 前跑相關測試；`tests/architecture` 與 `test_design_docs`、`test_agents_skill_sync`、`test_env_example`、`test_launch_plan` 先列為必跑。
- **舊值 `standard` 殘留**：任何漏掉 `parse_tier` 的讀取點會把它當未知值而退回 `safe`（偏保守），不會放寬；以一支全碼庫掃描的守門測試（禁止比較 `== "standard"`）降低遺漏。
- **展示時間緊**（11-23 前）→ 切批時把「遊戲內提示」與「創作頁勾選」排在最後，來不及時可晚一天而不阻擋展示；個人頁開關、目錄全可見、緊急煞車是展示的必要部分。
- **模型端仍可能拒絕**：關閉安全模式並不保證模型產出露骨內容（RPG 用免費模型）；提示措辭、條款與驗收說明都明寫不保證，且不附加引導詞、不改寫玩家內容。
- **免費模型的資料留存**：安全模式關閉時的輸入輸出可能被供應商留存或審閱；以條款與隱私權政策揭露（D12），上線給真實會員前仍須依 `launch-gates` 重新評估。
- **簡報衝突**：`docs/2.0簡報.md` 與六張圖解寫「規範兩種、程式仍三種」。依 deck-conflict-watch 只通知使用者、不自行修改；實作每批收尾時列出位置與選項。
- **緊急煞車的可操作性**：只靠環境變數與重啟，無法即時（秒級）停用；現階段為個人展示，接受此限制（見 Open Questions）。

## Migration Plan

1. 部署前：備份（既有 `aws_demo.py` 的資料庫快照步驟）。
2. 遷移 0020 → 部署新版映像（`--tag` 明確帶）→ `flask seed-scripts` → 重啟 → 驗證（`/healthz`、`/readyz`、`/account` 區塊、目錄全可見、關閉安全模式後劇本詢問與提示）。
3. 驗收完成前不提高展示環境的 `SAFE_MODE_FORCED`；若要在展示期間緊急停用，於主機環境檔設為 `true` 並重啟兩個池。
4. 回復：`aws_demo.py rollback` 換回上一個映像；遷移不降級（新欄位對舊程式無害）。

## Open Questions

- 緊急煞車若日後需要「不重啟即時停用」，可改讀資料庫設定表（`runtime_settings`，每次現讀）；這個 change 不做，待上線前評估。
