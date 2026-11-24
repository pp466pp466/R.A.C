# Tasks

## 1. 伺服器：預設頁面依角色決定

- [x] 1.1 `app/security/redirects.py`：新增常數 `ADMIN_AFTER_LOGIN = "/admin"`（與 `DEFAULT_AFTER_LOGIN` 並列，只定義在此檔）；`landing_for` 在沒有受限登入狀態時，以 `user.is_active_admin` 選預設頁面，並在 `safe_next` 的結果恰為 `DEFAULT_AFTER_LOGIN` 時對管理員回 `ADMIN_AFTER_LOGIN`；更新模組開頭的說明（不再是「只需要改一個常數」）。受限登入狀態與玩家的分支一行都不動。驗證：`pytest tests/test_landing.py tests/test_safe_next.py` 既有的玩家情境全數照常通過。
- [x] 1.2 `tests/test_landing.py` 新增管理員情境並把 `test_an_admin_without_age_data_is_not_sent_to_the_age_step` 的期望改為 `ADMIN_AFTER_LOGIN`：①管理員沒有 `next` 為 `/admin`；②`next` 為 `/game` 視為沒有指定（`/admin`）；③合法的明確 `next`（`/game/<id>`、`/account?tab=security`、`/cs`）照原樣回去；④不合法的 `next`（外部網址、`//`、`/\`、`/%2f/`、`None`、數字、空字串）為 `/admin`；⑤停權（狀態非啟用）的管理員不是 `/admin`（回 `DEFAULT_AFTER_LOGIN`，與後台權限同一個定義）；⑥等待刪除的管理員仍去 `/account`；⑦同樣情境的玩家結果不變；⑧`/` 與 `/login` 對已登入的管理員導向 `/admin`、對玩家導向 `/game`；⑨`ADMIN_AFTER_LOGIN == "/admin"` 與 `DEFAULT_AFTER_LOGIN == "/game"` 兩個常數各有斷言。驗證：`pytest tests/test_landing.py` 通過。
- [x] 1.3 `tests/test_admin_two_step_login.py`：密碼驗證成功沒有返回位置時 `next` 與頁面導向由 `/game` 改為 `/admin`（現有第 87、125 行）；返回位置是外部網址時回 `/admin`（第 109 行）；明確的 `/admin` 與其他合法返回位置的既有案例不變，另新增一個管理員被擋在 `/game/<存檔>` 後登入、回到該存檔的案例；`/age` 與 `/appeal` 對已完成者與沒有申訴狀態的管理員導向 `/admin`（沿用 `tests/test_age_page.py`、`tests/test_appeal_api.py` 的作法，只新增管理員案例，玩家案例不動）。驗證：`pytest tests/test_admin_two_step_login.py tests/test_age_page.py tests/test_appeal_api.py tests/test_google_endpoints.py` 通過（`test_google_endpoints.py` 都是玩家情境，預期不需改）。
- [x] 1.4 修訂 AGENTS.md 第 10 節「登入與 session」一條中的預設頁面一句：由「單一常數；目前為 /game，……客服 /cs 仍可直接開啟」改為「依角色各一個常數：玩家為 /game（RPG 的入口：選擇劇本與我的存檔，不直接進入聊天），管理員為 /admin；登入前指定的合法返回位置優先；客服 /cs 仍可直接開啟」，其餘句子不動；同步 `docs/帳號與安全-auth-security.md`（第 93 行的順序圖說明、第 103 行的預設頁面段落）與 `docs/系統架構-architecture.md` 第 144 行 `landing_for` 一列。驗證：`pytest tests/test_agents_skill_sync.py tests/test_design_docs.py` 通過；`git diff AGENTS.md` 只動那一句。

## 2. 前端：密碼驗證頁的備援與後台頂列

- [ ] 2.1 `app/static/js/auth/login-password.js` 的備援導向由 `'/game'` 改為 `'/admin'`（`Auth.safeDestination(response.data && response.data.next, '/admin')`）；`auth-flows.js` 的 `DEFAULT_PAGE` 維持 `'/game'`。`tests/test_auth_pages.py` 的前端備援比對（現有第 293 至 303 行）改為兩組：`auth-flows.js` 與 `age.js` 對 `DEFAULT_AFTER_LOGIN`、`login-password.js` 對 `ADMIN_AFTER_LOGIN`；說明文字同步。驗證：`pytest tests/test_auth_pages.py` 通過，且把備援值改回 `'/game'` 時該測試失敗。
- [ ] 2.2 `app/templates/admin/console.html` 頂列在「客服」之前加 `<a class="admin-topbar__link" href="/game">冒險</a>`（不新增 CSS、不用 `data-bs-*`）；`tests/test_admin_page.py` 新增：頂列依序有「冒險」`/game`、「客服」`/cs`、「帳號」`/account` 三個連結，且頂列沒有 `data-bs-` 與 `v-html`。驗證：`pytest tests/test_admin_page.py` 通過。
- [ ] 2.3 同步前端文件：`docs/前端結構-frontend.md`（第 119 行流程圖「其他 → /cs（預設頁面）」是過時的，改為依角色的 `/game`／`/admin`；第 212 行預設頁面一句；登入頁與管理員密碼驗證頁的說明；`/admin` 頂列的連結）與專案 `README.md` 的登入頁、年齡頁、申訴頁各列與第 173、1014 行「單一常數」的說明。驗證：`pytest tests/test_design_docs.py tests/test_readme.py` 通過。

## 3. 整合檢查

- [ ] 3.1 變異驗證（在 scratchpad 的專案副本上做，設 `--test-timeout` 與行程逾時，專案檔案雜湊前後相同）：拿掉 `is_active_admin` 判斷（改成永遠 `DEFAULT_AFTER_LOGIN`）、拿掉「等於 `/game` 視為沒有指定」、把 `ADMIN_AFTER_LOGIN` 改成 `/game`、對玩家也回 `/admin`、把 `is_active_admin` 換成 `role == "admin"`、拿掉頂列的「冒險」連結、把 `login-password.js` 備援改回 `/game`，每個變異至少一個測試失敗。驗證：存活的變異做差分判斷（等價或補測試），結果寫入任務完成註記。
- [ ] 3.2 全套測試（本 change 動了 `app/` 後端與登入導向）：背景執行 pytest 全套（輸出存檔、不接 tail）與 `node --test tests/js/*.test.js`。驗證：0 失敗；略過的項目與變更前相同。
- [ ] 3.3 實機驗證（自有實例，全新資料庫、假的 Google 供應商、`127.0.0.1`）：管理員走完 Google 與密碼兩步驗證落在 `/admin`；已登入的管理員打開 `/` 與 `/login` 導向 `/admin`；管理員被擋在 `/game/<存檔>` 後登入回到該存檔；玩家登入落在 `/game`、打開 `/admin` 得 404；`/admin` 頂列「冒險」開啟 `/game`；管理員直接開 `/cs` 不被導走。驗證：逐項記錄結果於 README 的驗收紀錄。
- [ ] 3.4 依 `deck-conflict-watch` 技能檢查與 `docs/2.0簡報.md`、六張圖解的衝突（預期有：「網站入口」章節與登入圖、入口圖的產生器寫「否則 `/game`」「`DEFAULT_AFTER_LOGIN`＝`/game`」）；**只通知，不自行修改簡報、圖解、PDF、pptx**，由使用者決定。驗證：檢查結果寫入提交說明與回覆。
- [ ] 3.5 依 `docs/README.md` 的對照表複查所有受影響的設計文件（`docs/路線圖-roadmap.md` 加本 change 的狀態與日期、各文件標頭的「最後更新」與「對應 change」），並讓 `tests/test_design_docs.py` 通過。驗證：`pytest tests/test_design_docs.py tests/test_readme.py` 通過。

## 批 1 完成註記（2026-11-24；只跑相關測試加全域守門子集，**未跑全套 pytest**）

- **範圍**：1.1、1.2、1.3，加 1.4（`docs/帳號與安全-auth-security.md` 的順序圖說明與預設頁面段落、`docs/系統架構-architecture.md` 的 `landing_for` 一列，兩份標頭的「對應 change」；AGENTS.md 第 10 節預設頁面那一句，使用者核可後**單獨一個提交**，在批 1 之前提交）。AGENTS.md 的修訂必須連同它的鏡像一起提交，否則 `test_agents_skill_sync` 會紅：`.claude/skills/python-flask-skill/references/auth-security.md` 與 `tests/test_agents_skill_sync.py` 第 70 行寫死的片段（兩處都是同一句的逐字複本）；`git diff AGENTS.md` 只動那一句。
- **實作**：`app/security/redirects.py` 新增 `ADMIN_AFTER_LOGIN = "/admin"`；`landing_for` 在沒有受限登入狀態時，已驗證的 `next` 恰為 `DEFAULT_AFTER_LOGIN` 且 `user.is_active_admin` 時回 `ADMIN_AFTER_LOGIN`，其餘照舊（受限登入狀態與玩家的分支沒有動）；模組與函式說明已更新。呼叫者一處都沒改。
- **測試**：`test_landing.py`（新增 11 個函式，其中 2 個參數化；原本的管理員案例期望值改為 `ADMIN_AFTER_LOGIN`）、`test_admin_two_step_login.py`（第 87、109 行的期望由 `/game` 改為 `/admin`——任務文字寫的第 125 行其實是玩家案例，沒有動；新增管理員被擋在 `/game/<存檔>`、`/cs`、`/account` 後回到原處、恰為 `/game` 視同沒有指定、玩家不受影響共 3 項）、`test_age_page.py`（管理員未確認與已確認各一項導向 `/admin`）、`test_appeal_api.py`（管理員開 `/appeal` 導向 `/admin`）。`test_google_endpoints.py`、`test_safe_next.py` 沒有修改。
- **驗證**：相關測試（test_landing、test_safe_next、test_admin_two_step_login、test_age_page、test_appeal_api、test_google_endpoints、test_restricted_session）加全域守門子集（tests/architecture、test_service_errors、test_design_docs、test_readme、test_legal_pages、test_agents_skill_sync）合計 1,360 通過、2 略過（`test_no_legacy_tier_checks` 的設計性略過）、0 失敗。**未跑**：全套 pytest、Node 測試、變異驗證、實機驗證（留批 3）；前端（批 2）尚未動，`login-password.js` 的備援仍是 `/game`。
- **簡報**：依 `deck-conflict-watch`，程式已改變預設頁面行為，2.0 簡報「網站入口」章節與登入圖、入口圖產生器的「否則 /game」「`DEFAULT_AFTER_LOGIN`＝/game」現在已過時；依任務 3.4 只通知，不改。

## Workflow follow-up

- 使用者驗收通過後才封存（`openspec archive`；封存時 `auth-pages` 的修改與新增、`admin-console` 的新增同步到主規格）；封存後更新 `docs/路線圖-roadmap.md` 與 HANDOFF。
- 部署到展示環境由使用者決定（沒有遷移，只換映像；回復只換回前一版映像）。
