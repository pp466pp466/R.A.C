# Proposal

## Why

AGENTS.md（第 7、10、11 節，2026-11-17 修訂）已把內容分級改成兩種模式：**安全模式開**（預設）與**安全模式關**（成年角色之間合意的露骨內容），全站只限滿 18 歲；`adult-only-signup`（已封存）完成了「只限滿 18 歲」。但程式與規格仍是三種模式（`safe／standard／adult`）、部署鎖是反向的 `ADULT_MODE_ENABLED`（預設關、須列出書面許可涵蓋的模型）、目錄依模式過濾劇本、存檔在開檔當下釘住模式，而且**前端沒有任何畫面能切換內容模式**（`PUT /api/account/content-mode` 沒有任何呼叫端）：新玩家預設安全模式卻無法離開。

11-24、11-25 要實際報告並測試全部功能，所以政策要在 11-23 前部署到展示環境。

## What Changes

- **兩種模式取代三種**：`safe`（安全模式開，預設）與 `adult`（安全模式關）。舊值 `standard` 由新程式一律當作 `adult` 讀取（expand/contract，資料改寫是獨立步驟）；不再有「一般模式」。
- **生效模式 ＝ 玩家目前的設定、劇本是否綁定安全模式、緊急煞車三者中最保守者**，每次需要時現算；**存檔不再釘住開檔當下的模式**（`sessions.content_tier` 不再被任何政策判斷讀取）。
- **個人頁的安全模式開關**：關閉時跳出自製 Modal，只需同意安全規範（記錄確認時間與安全規範版本）；不再勾選「我確定已滿 18 歲」。開啟（回到安全模式）不需要確認。安全規範版本換新，舊的同意失效。
- **緊急煞車取代部署鎖**：新環境變數 `SAFE_MODE_FORCED`（預設不強制，即可關閉安全模式）；開啟時所有有效模式一律為安全。移除 `ADULT_MODE_ENABLED` 與 `ADULT_MODE_MODELS`（書面許可、模型涵蓋清單都不再是前提），舊變數被忽略並於啟動時警告；`staging`／`production` 缺值不影響啟動。
- **所有劇本對所有玩家可見**：目錄不再依模式過濾，開存檔、創角資料、主角生成不再因「劇本模式高於玩家模式」而回 403 `content_tier_restricted`。
- **劇本綁定安全模式**：作者可勾選「綁定安全模式」，綁定的劇本不論玩家設定一律以安全模式遊玩；預設未綁定。既有已發佈版本：`safe`＝已綁定，`standard`／`adult`＝未綁定。創作頁提示安全模式的限制，目錄卡片標示綁定。
- **目錄通知**：安全模式開的玩家點選未綁定的劇本時，系統跳出通知詢問是否修改安全模式；確定則跳轉到個人頁的安全模式設定，改完回到該劇本，取消則維持安全模式繼續。
- **遊戲內提示**：安全模式下玩家輸入被規則層判為性內容、或模型輸出被審查層因性內容擋下或降成安全尺度時，在生成之後提示玩家可以嘗試關閉安全模式；措辭不保證結果；每個存檔最多一次、可關閉；不自動調高。緊急煞車開啟時不提示。
- **安全開的尺度沿用原「安全模式」的嚴格尺度**；安全關＝原成人模式的尺度；原「一般模式」的提示詞與審查尺度移除。四類絕對禁止項（未成年的性內容、性暴力、非自願、以真人名義）不論模式一律由伺服器端規則層擋下，不依賴模型拒絕，不變。
- **條款與隱私權政策**補上：服務含露骨內容（安全模式關閉時）、須年滿 18 歲、謊報年齡者自行承擔責任、敏感內容可能被模型供應商留存或審閱；條款版本提高。
- **展示用內建劇本**（使用者 2026-11-19 選定）：把內建劇本 `xianxia-qingyun` 的資料檔改為未綁定，`flask seed-scripts` 會新增第 2 版取代第 1 版（已開的存檔不受影響）。
- **移除**：遊戲端「未滿 18 歲固定使用安全模式」的說明（`MINOR_NOTE` 等已無法到達的分支）、`AccessPolicy` 的年齡上限分支、`is_adult` 摘要欄位、`confirm_adult` 參數、`content_mode_restricted` 與 `adult_mode_unavailable` 錯誤、`ContentTierRestricted`。

**BREAKING**（對外 API，皆為本站前端與測試的使用者）：`PUT /api/account/content-mode` 的 `mode` 只接受 `safe`／`adult`，`confirm_adult` 不再需要；目錄回應的 `content_mode` 移除 `is_adult`；`content_tier_restricted`、`content_mode_restricted`、`adult_mode_unavailable` 不再回傳；`ADULT_MODE_*` 環境變數失效。

## Capabilities

### New Capabilities
- `account-safe-mode-setting`: 個人頁（`/account`）的安全模式開關、關閉時的同意 Modal、緊急煞車下的停用說明，以及從目錄通知跳轉後回到原劇本的流程。
- `script-safe-binding`: 劇本「綁定安全模式」的語意（以劇本內容的 `content_tier` 表示：`safe`＝綁定、`adult`＝未綁定，舊值 `standard` 讀作未綁定）、創作頁的勾選與限制提示、目錄卡片的標示。
- `safe-mode-upgrade-hint`: 安全模式下的遊戲內提示：觸發條件、每存檔至多一次的紀錄、措辭限制、緊急煞車時不提示，以及不自動調高。

### Modified Capabilities
- `content-tier-modes`: 三種模式改兩種、生效模式取三者最保守、同意只要安全規範、API 改形、部署鎖改緊急煞車、舊變數淘汰；移除年齡上限相關需求。
- `age-confirmation`: 條款與隱私權政策補上露骨內容、18 歲、謊報責任與供應商留存的揭露。
- `auth-pages`: `/account` 新增安全模式區塊。
- `rpg-sessions`: 開存檔不再因劇本模式拒絕；存檔不記錄也不釘住模式。
- `rpg-entry-api`: 目錄含全部上架劇本、卡片帶是否綁定安全模式、`content_mode` 摘要改形、我的存檔的模式改為現算。
- `rpg-entry-pages`: 內容模式提示改為安全開／關說明，並新增目錄通知的詢問流程。
- `rpg-creation-wizard`: 精靈頁不再有「模式高於目前」的 403 說明。
- `rpg-protagonist`: 開存檔與創角資料 API 不再因劇本模式回 403。
- `protagonist-suggestion`: 生成不再因劇本模式回 403，提示詞的模式為現算的生效模式。
- `rpg-game-screen`: 遊戲頁顯示現算的生效模式（移除未成年說明），並接收遊戲內提示。
- `rpg-output-review`: 審查尺度只有安全開與安全關兩種。
- `rpg-turn-context`: 系統指令的分級界線只有兩段。
- `rpg-turn-engine`: 玩家行動的規則層與提醒措辭（只提醒、不調高）對應兩種模式。
- `script-authoring`: 宣告的模式不再受作者年齡上限限制（全站皆成年），改為綁定的選擇。
- `script-fixed-spec`: `content_tier` 的合法值與舊值相容。
- `script-review`: 「宣告」的語意改為綁定；未綁定的劇本適用原 `standard`／`adult` 的未成年角色處理。
- `script-store`: 公開清單不再依內容模式篩選。
- `builtin-script-seed`: 首發劇本改為未綁定。
- `launch-gates`: 關卡清單移除「書面許可」前提，改列露骨內容揭露的法務覆核。

## Impact

- **程式**：`app/models/user.py`（`CONTENT_TIERS`）、`app/services/content_tier.py`、`content_mode_service.py`、`access_policy.py`（移除年齡上限分支、加緊急煞車）、`startable.py`、`session_service.py`、`game_service.py`（三處 `effective_tier(save_tier=…)`）、`game_entry_service.py`、`rpg_prompt.py`（`TIER_BOUNDARIES`）、`script_rules.py`／`rpg_output_rules.py`／`rpg_output_guard.py`、`script_spec.py`、`script_review_prompt.py`、`script_repository.py`（目錄篩選）、`app/config.py`（`ADULT_MODE_*` → `SAFE_MODE_FORCED`）、`app/services/errors.py`、legal 範本（`/terms`、`/privacy`）、種子資料 `xianxia-qingyun.json`。
- **前端**：`/account` 範本與 `account.js`（安全模式區塊與同意 Modal）、`game-labels.js`、`game-state.js`、`game-session.js`、`entry.js`、`wizard*.js`、`script-draft.js`、`script-workshop.js`、`workshop.html`、`game/index.html`，及對應 Node 測試。
- **資料庫**：遷移 0020（expand：`sessions` 新增可為空的 `safe_hint_at`；不改既有欄位名；資料改寫獨立步驟）；`docs/project資料結構文件.md` 須同步並在第 14 節追加異動紀錄。
- **設定與部署**：`.env.example`、`scripts/launch_plan.py`、展示環境的環境檔（`aws_demo.py` 的主機檔）；部署前備份並做資料庫快照，展示環境升到新的遷移頭。
- **文件**：`docs/RPG.md`、`帳號與安全-auth-security.md`、`系統架構-architecture.md`、`前端結構-frontend.md`、`營運與部署-operations.md`、`路線圖-roadmap.md`、README 驗收紀錄；**簡報衝突**（`docs/2.0簡報.md` 與六張圖解寫「規範兩種、程式仍三種」）依 deck-conflict-watch 只通知使用者、不自行修改。
- **時程**：11-23 前部署到展示環境；若來不及，第 ⑤ 批的遊戲內提示與第 ④ 批的創作頁勾選可晚一天（見 tasks）。
