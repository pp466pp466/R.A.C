# Design

## Context

動機與範圍見 proposal.md。目前的實作（已讀過程式確認）：

- `AgePolicy`（`app/services/age_policy.py`）是年齡規則的單一來源：最低註冊年齡與最低使用年齡預設 13、成年門檻 18、七個年齡區間、政策版本字串。`validate_age_input` 對「年齡低於最低註冊年齡，或所選區間整段都低於它」回 `age_not_eligible`；`AuthService.verify_age` 遇到它就呼叫 `AccountErasureService.erase_underage`（管理員除外）。程式本來就預期「日後把最低年齡改為 18」只換設定與版本。
- 登入頁只有條款勾選：前端送 `accept_terms`，`GoogleSignInService.begin` 把旗標放進簽章的交易 cookie（鍵 `t`），`_resolve` 在沒有帳號時檢查，沒勾就以 `ERROR_TERMS` 拒絕並記 `auth.google_rejected.terms`；`LOGIN_ERROR_MESSAGES` 把錯誤碼轉成登入頁的提示。
- 帳號有 `terms_version`、`terms_accepted_at`；年齡欄位（`birth_date`、`age_range`、`age_confirmed_at`、`age_policy_version`、`is_adult_at_confirmation`）都可為空，補填時以條件式 UPDATE 一次寫入，並依確認當下的年齡設定 `content_tier_preference`（成年 `standard`、未成年 `safe`）。
- `/account` 在 `age_group === 'minor'` 時顯示安全模式提示。既有帳號只有管理員（已確認）。
- 資料表與欄位變更要同步 `docs/project資料結構文件.md` 與 `tests/test_project_db_schema_doc.py`；migration 採 expand/contract，不得含原生 SQL。

## Goals / Non-Goals

**Goals:**
- 最低年齡 18 在「註冊」與「使用」兩個閘門都生效，未滿 18 歲者的帳號與個資立即抹除、不留生日。
- 新使用者在登入頁明確聲明年滿 18 歲，聲明的時間與版本留下紀錄，且聲明只是記錄、不是年齡判斷的依據。
- 沒有未成年的特例殘留在程式、畫面與規格裡（`age_group = minor` 的提示、`is_adult_at_confirmation` 的寫入、預設 `standard`）。

**Non-Goals:**
- 不改內容分級的模式數量、安全模式開關、劇本綁定、緊急煞車、遊戲內提示（`safe-mode-toggle`）。`AccessPolicy` 依年齡決定內容上限的程式碼保留（在最低年齡 18 之下不會被觸發），由那個 change 一併移除。
- 不改年齡區間選項（保留「未滿 13 歲」「13–17」讓未成年者自行表明並被拒絕）。
- 不加強年齡驗證方式（仍是自行聲明，AGENTS.md 第 10 節）。
- 不處理露骨內容與供應商留存的政策告知（見決策 D8）。

## Decisions

**D1　最低年齡只改 `AgePolicy` 的預設值並提高版本。**
`min_register_age`、`min_use_age` 改為 18（`adult_age` 本來就是 18），版本字串改為 `2026-11-17-adults-only`。不新增設定項或環境變數：規則本來就集中在這裡，頁面與端點都向它取值。
替代方案：用環境變數控制最低年齡 → 否決，年齡規則是產品政策不是部署差異，AGENTS.md 也要求單一來源與版本化。

**D2　區間選項不變，兩個未成年選項合併成同一種拒絕。**
選到「未滿 13 歲」或「13–17」，或生日算出未滿 18 歲，都走 `age_not_eligible`（既有的判斷式「所選區間整段低於最低註冊年齡」自然涵蓋）。錯誤文案由 `AgePolicy` 的最低年齡組成（「本服務僅供 18 歲以上使用，無法註冊。」）。
替代方案：把兩個選項合成一個「未滿 18 歲」→ 否決（使用者要求年齡確認頁照舊；合併會改變年齡統計的區間代碼，沒有收益）。

**D3　年滿 18 歲聲明記在兩個新的可為空欄位。**
`users.adult_declared_at`（UTC 時間）與 `users.adult_declaration_version`（`Unicode(40)`，值為當時的 `AgePolicy.version`，由伺服器決定）。遷移 `0019` 只新增、可為空、不回填、不加索引（沒有任何查詢依賴它們）；管理員與升級前的帳號維持 NULL，任何閘門都不讀它。
為什麼不重用 `terms_accepted_at`／`terms_version`：AGENTS.md 要求記錄「年滿 18 歲聲明的時間與版本」，條款版本與年齡政策版本是兩個獨立演進的字串，混在一起日後無法分辨使用者當時聲明的是什麼規則。
為什麼聲明不是閘門：年齡一律由 `birth_date` 於當下計算（AGENTS.md）；聲明是「使用者自己說了什麼」的稽核紀錄，與生日不一致時以生日為準。
遷移依專案慣例：`op.batch_alter_table`、ORM 型別、沒有原生 SQL；downgrade 用 `recreate="never"` 直接移除欄位（同 0017）。

**D4　登入發起請求多一個 `declare_adult` 旗標，放進同一個交易 cookie。**
`/api/auth/google/start` 接受 `accept_terms` 與 `declare_adult`（皆須嚴格等於 `true`）；交易內容加一個旗標（`a`）。`_resolve` 在「沒有帳號」的分支依序檢查：條款未勾 → `ERROR_TERMS`；聲明未勾 → 新的 `ERROR_ADULT = "adult_required"`，記錄新事件 `auth.google_rejected.adult`。兩者都只在建立新帳號時檢查，回訪者與合併既有帳號的路徑不受影響。建立帳號時一併寫入 `adult_declared_at`（`now`）與 `adult_declaration_version`。前端傳入的任何時間或版本欄位一律忽略（請求只讀兩個布林）。
部署瞬間仍在進行中的授權流程（交易 cookie 沒有 `a`）會被視為未聲明而被拒絕並提示重新勾選；影響極小（交易 cookie 的有效期很短），不做相容。
替代方案：把兩項合併成一個勾選框 → 否決，使用者要求「在註冊時多一個確認」，且合併後無法分別記錄與提示。

**D5　預設內容模式改為一律 `safe`，停寫成年紀錄。**
`UserRepository.confirm_age` 的條件式 UPDATE 不再寫 `is_adult_at_confirmation`（欄位保留、不再寫入，日後的 contract 遷移再移除），`content_tier_preference` 固定為 `safe`。簽名上的 `is_adult` 參數移除，`VerifiedAge.is_adult` 保留（由生日算出，只給呼叫端內部使用），避免大範圍改動。這讓新帳號符合 AGENTS.md「預設為安全模式開」，同時不動三種模式的結構（玩家仍可用現有的個人頁開關調整，成人模式的既有門檻不變）。
這是一次可見的行為改變：新的成年玩家不再預設 `standard`。已存在的帳號（只有管理員）不受影響。

**D6　`age_group` 保留在 `/api/auth/me`，但不再有 `minor`。**
`age_group()` 函式與回應欄位照舊（前端與測試相依），已完成確認者必為 `adult`。刪除的是畫面：`account.html` 的 `ageGroup === 'minor'` 區塊與 `account.js` 對應的狀態。`GROUP_MINOR` 常數保留給 `AgePolicy` 的泛用判斷。
替代方案：連欄位一起移除 → 否決，會牽動 API 契約與多個測試，而沒有使用者可見的收益。

**D7　`AccessPolicy` 不改程式，只靠最低使用年齡自然拒絕。**
`min_use_age = 18` 之後，已有生日但未滿 18 歲的帳號（資料庫裡不存在，只在單元測試裡製造）會得到既有的 `AgeRestricted`。依年齡決定內容上限的分支變成無法到達，留給 `safe-mode-toggle` 刪除，避免兩個 change 同時改同一段程式。

**D8　條款與隱私權政策只寫「年齡」，不寫「露骨內容」。**
這個 change 在 `/terms` 加入僅限滿 18 歲、如實聲明與自行承擔責任、未滿 18 歲立即刪除；`/privacy` 把生日用途的「內容分級」拿掉並把 13 歲改 18 歲。「服務含露骨內容」與「敏感內容可能被模型供應商留存」要等到安全模式可關閉真的成為功能（`safe-mode-toggle`）才寫，否則在功能出現前就對使用者作不實的陳述。條款版本現在提高一次，`safe-mode-toggle` 會再提高一次。

**D9　測試玩家簽發要帶聲明。**
`scripts/mint_test_sessions.py` 產生的玩家代表「已完成註冊與年齡確認的成年玩家」，要同時寫入 `adult_declared_at` 與 `adult_declaration_version`，與真實登入產生的帳號一致；個人資料匯出納入這兩個欄位（`personal-data-export` 規格），NULL 時匯出空值。

**D10　遷移編號與後續 change。**
本 change 的遷移是 `0019_adult_declaration`。尚未立案的 `rpg-ops-loadtest` 若需要遷移，編號順延為 `0020`；`safe-mode-toggle` 的分級值遷移排在其後。部署順序依專案流程：備份（展示環境為手動快照）→ migration → 新版程式。

## Risks / Trade-offs

- **自行聲明的法律強度有限** → 沿用 AGENTS.md 第 10 節的既定做法（生日加區間加勾選，謊報者自行承擔責任寫在條款），日後若法務要求更強的方式，只換確認方式。聲明有稽核紀錄（時間與版本）。
- **未滿 18 歲者填過的資料被抹除，無法復原** → 這是規範要求（不保存生日、立即抹除，與先前未滿 13 歲相同）；抹除前不寄任何信、不留暱稱。
- **展示環境上真人測試帳號** → 2026-11-24、11-25 展示前，驗收用的 Google 帳號走一般的抹除出口清除（第 7 批的做法），管理員帳號保留。
- **預設模式改為 `safe`** → 新玩家不再預設一般模式，展示時戀愛向內容會較保守；玩家可在個人頁自行切換，`safe-mode-toggle` 之後才有「關閉安全模式」的完整流程。展示前要向使用者說明這個差異。
- **舊的授權流程被拒** → 見 D4，影響只在部署瞬間。
- **規格中還留有「13 至 17 歲玩家」的遊戲情境**（`rpg-entry-api`、`rpg-entry-pages`、`rpg-game-screen`、`rpg-sessions`）→ 這些玩家被年齡閘門擋下，情境不會發生；由 `safe-mode-toggle` 一併移除，這裡不改以免兩個 change 競爭同一批規格。

## Migration Plan

1. 本機：遷移 `0019`（升級、降級、再升級）在 SQLite 與以 SQL Server 方言編譯的檢查都通過；`tests/test_project_db_schema_doc.py` 與文件同步。
2. 展示環境（雪梨，`aws_demo.py` 流程）：先建立部署前手動快照 → 對 RDS 執行 migration 到 `0019` → 部署新映像（兩個池）→ `verify`。舊版程式在新結構上可運作（兩欄可為空）。
3. 回復：`aws_demo.py rollback` 回到上一個映像標籤；新欄位留著不影響舊版程式。資料庫降級只在沒有真實資料時使用（展示環境的資料可重建）。
4. 驗收（使用者在場，Google 登入為人工）：新帳號不勾第二項被拒、兩項都勾後進年齡確認頁、填 15 歲生日被抹除且重新登入可再註冊、填 25 歲生日通過並預設為安全模式、回訪者一鍵登入。驗收用的帳號驗後抹除。
